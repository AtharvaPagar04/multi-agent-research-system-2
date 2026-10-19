"""
evidence/store.py
-----------------
Append-only, atomic file-backed store for EvidenceRecord objects.

Guarantees & Invariants
-----------------------
1. Atomic Persistence:
   flush() writes to a temporary sibling file and commits via os.replace(),
   guaranteeing all-or-nothing writes even on process interruption.
2. Run Isolation:
   Each store uses a unique UUID4 run_id. Attempting to flush onto an existing
   file with a different run_id raises FileExistsError.
3. Safe Duplicate-Source Behavior:
   - Identical fetches for the same URL are idempotent no-ops.
   - Differing search snippets for the same URL (e.g. from Exa and Tavily) are merged,
     preserving all snippet observations and provider provenance without crashing.
   - An existing SNIPPET_ONLY record is upgraded when a FULL_TEXT fetch arrives,
     preserving the earlier search snippets in snippet_text and snippet_observations.
   - A later SNIPPET_ONLY fetch never downgrades an existing FULL_TEXT record.
   - A FAILED fetch never overwrites a successful retrieval.
   - Conflicting FULL_TEXT content for the same URL raises ValueError to prevent
     silent replacement of full-text source content.
4. Strict Load Validation:
   load() verifies top-level schema keys, version, record list types, exact record count,
   record-level required keys, valid enum statuses, and rejects duplicate IDs.
"""

from dataclasses import replace
import json
import logging
import os
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple
import uuid
from datetime import datetime, timezone

from research_crew.evidence.model import (
    EvidenceRecord,
    RetrievalStatus,
    make_content_hash,
    resolve_trusted_title,
)

logger = logging.getLogger(__name__)

_STORE_VERSION: int = 1


def generate_run_id() -> str:
    """Return a unique 32-hex-char run identifier (UUID4, no hyphens)."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------

class EvidenceStore:
    """In-memory index of EvidenceRecord objects backed by an atomic JSON file."""

    def __init__(
        self,
        run_id: str,
        filepath: Path,
        records: Optional[Dict[str, EvidenceRecord]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        self.run_id: str = run_id
        self.filepath: Path = filepath
        self._records: Dict[str, EvidenceRecord] = records or {}
        self.created_at: str = (
            created_at or datetime.now(timezone.utc).isoformat()
        )

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, output_dir: Path = Path(".")) -> "EvidenceStore":
        """Create a new store with a unique run ID and backing file path.

        Raises FileExistsError if the target file already exists.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = generate_run_id()
        filepath = output_dir / f"evidence_{run_id}.json"
        if filepath.exists():
            raise FileExistsError(
                f"Evidence store file already exists (UUID collision): {filepath}"
            )
        logger.info("New evidence store: run_id=%s path=%s", run_id, filepath)
        return cls(run_id=run_id, filepath=filepath)

    @classmethod
    def load(cls, filepath: Path) -> "EvidenceStore":
        """Load and strictly validate an existing evidence store from a JSON file.

        Raises
        ------
        FileNotFoundError:
            If filepath does not exist.
        ValueError:
            If JSON is malformed, required fields are missing, status is invalid,
            record count is inconsistent, or duplicate IDs exist.
        json.JSONDecodeError:
            If file is not valid JSON.
        """
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Evidence store file not found: {filepath}")

        with filepath.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid store JSON: top-level element must be a dict, got {type(data).__name__}"
            )

        # 1. Top-level required fields
        required_header = ["version", "run_id", "created_at", "record_count", "records"]
        for field in required_header:
            if field not in data:
                raise ValueError(f"Invalid store JSON: missing required field {field!r}")

        stored_version = data["version"]
        if stored_version != _STORE_VERSION:
            raise ValueError(
                f"Unsupported evidence store version: {stored_version!r} "
                f"(expected {_STORE_VERSION})"
            )

        raw_records = data["records"]
        if not isinstance(raw_records, list):
            raise ValueError("Invalid store JSON: 'records' field must be a list")

        # 2. Inconsistent record count check
        expected_count = data["record_count"]
        if len(raw_records) != expected_count:
            raise ValueError(
                f"Inconsistent record count: header specifies {expected_count}, "
                f"but found {len(raw_records)} records"
            )

        # 3. Record validation and duplicate ID check
        records: Dict[str, EvidenceRecord] = {}
        for idx, rec_data in enumerate(raw_records):
            if not isinstance(rec_data, dict):
                raise ValueError(f"Record at index {idx} must be a dict")

            # EvidenceRecord.from_dict validates required fields and retrieval_status
            record = EvidenceRecord.from_dict(rec_data)

            if record.evidence_id in records:
                raise ValueError(
                    f"Duplicate evidence_id found in loaded store: {record.evidence_id!r}"
                )
            records[record.evidence_id] = record

        logger.info(
            "Loaded evidence store: run_id=%s records=%d path=%s",
            data.get("run_id"),
            len(records),
            filepath,
        )

        return cls(
            run_id=data["run_id"],
            filepath=filepath,
            records=records,
            created_at=data.get("created_at"),
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, record: EvidenceRecord) -> None:
        """Add or safely merge a record in the in-memory index.

        Safe duplicate behavior
        ------------------------
        - Identical records: idempotent no-op.
        - FAILED incoming: never overwrites existing SUCCESS (FULL_TEXT or SNIPPET_ONLY).
        - Upgrading from FAILED to SUCCESS: replaces FAILED with SUCCESS.
        - Upgrading from SNIPPET_ONLY to FULL_TEXT: upgrades record, retaining original
          search snippets in snippet_text and snippet_observations.
        - Differing SNIPPET_ONLY records: merges snippets into snippet_observations and
          captured_text without crashing or discarding useful data.
        - Downgrade attempt (FULL_TEXT to SNIPPET_ONLY): preserves FULL_TEXT and appends
          new snippet to observations.
        - Conflicting FULL_TEXT content: raises ValueError to prevent silent replacement.
        """
        if record.evidence_id in self._records:
            existing = self._records[record.evidence_id]

            # Reject true collision or mismatched URL
            if existing.source_url != record.source_url:
                raise ValueError(
                    f"Duplicate evidence_id={record.evidence_id!r} with conflicting URL: "
                    f"existing={existing.source_url!r}, new={record.source_url!r}"
                )

            # Scenario A: New fetch failed -> do not overwrite existing successful retrieval
            if record.retrieval_status == RetrievalStatus.FAILED:
                logger.info(
                    "Ignoring failed fetch for already retrieved URL: %s",
                    record.source_url,
                )
                return

            # Scenario B: Existing was failed, new is successful -> replace with success
            if existing.retrieval_status == RetrievalStatus.FAILED:
                logger.info(
                    "Replacing failed retrieval with successful fetch for URL: %s",
                    record.source_url,
                )
                self._records[record.evidence_id] = record
                return

            # Scenario C: Existing is snippet, new is full text -> upgrade
            if (
                existing.retrieval_status == RetrievalStatus.SNIPPET_ONLY
                and record.retrieval_status == RetrievalStatus.FULL_TEXT
            ):
                logger.info(
                    "Upgrading SNIPPET_ONLY to FULL_TEXT for URL: %s (preserving snippet provenance)",
                    record.source_url,
                )
                # Retain all observed search snippets
                combined_obs = list(existing.snippet_observations)
                if existing.captured_text and existing.captured_text not in combined_obs:
                    combined_obs.append(existing.captured_text)

                provider_chain = (
                    f"{existing.provider}->{record.provider}"
                    if existing.provider and record.provider
                    else (record.provider or existing.provider)
                )

                merged_title = resolve_trusted_title(
                    record.title or existing.title,
                    record.captured_text,
                )

                upgraded = replace(
                    record,
                    snippet_text=existing.captured_text,
                    snippet_observations=tuple(combined_obs),
                    provider=provider_chain,
                    final_url=record.final_url or existing.final_url,
                    title=merged_title,
                )
                self._records[record.evidence_id] = upgraded
                return

            # Scenario D: Existing is full text, new is snippet -> preserve full text
            if (
                existing.retrieval_status == RetrievalStatus.FULL_TEXT
                and record.retrieval_status == RetrievalStatus.SNIPPET_ONLY
            ):
                logger.info(
                    "Retaining FULL_TEXT over incoming SNIPPET_ONLY for URL: %s",
                    record.source_url,
                )
                combined_obs = list(existing.snippet_observations)
                if record.captured_text and record.captured_text not in combined_obs:
                    combined_obs.append(record.captured_text)

                snip_text = existing.snippet_text or record.captured_text
                self._records[record.evidence_id] = replace(
                    existing,
                    snippet_text=snip_text,
                    snippet_observations=tuple(combined_obs),
                )
                return

            # Scenario E1: Both are SNIPPET_ONLY
            if (
                existing.retrieval_status == RetrievalStatus.SNIPPET_ONLY
                and record.retrieval_status == RetrievalStatus.SNIPPET_ONLY
            ):
                if existing.content_hash == record.content_hash:
                    # Identical snippet: idempotent no-op
                    return

                # Differing snippets for the same URL (e.g. from Exa and Tavily)
                logger.info(
                    "Merging differing snippets for URL: %s (preserving both observations)",
                    record.source_url,
                )
                new_obs = list(existing.snippet_observations)
                if existing.captured_text and existing.captured_text not in new_obs:
                    new_obs.append(existing.captured_text)
                if record.captured_text and record.captured_text not in new_obs:
                    new_obs.append(record.captured_text)

                # Merge captured_text so substring validation matches passages from either snippet
                if record.captured_text in existing.captured_text:
                    merged_text = existing.captured_text
                elif existing.captured_text in record.captured_text:
                    merged_text = record.captured_text
                else:
                    merged_text = f"{existing.captured_text}\n\n{record.captured_text}"

                # Merge provider provenance
                providers = []
                for p in [existing.provider, record.provider]:
                    if p:
                        for part in p.split(","):
                            clean_part = part.strip()
                            if clean_part and clean_part not in providers:
                                providers.append(clean_part)
                merged_provider = ",".join(providers) if providers else None

                merged_record = replace(
                    existing,
                    captured_text=merged_text,
                    content_hash=make_content_hash(merged_text),
                    snippet_observations=tuple(new_obs),
                    provider=merged_provider,
                    title=existing.title or record.title,
                    final_url=existing.final_url or record.final_url,
                )
                self._records[record.evidence_id] = merged_record
                return

            # Scenario E2: Both are FULL_TEXT
            if existing.content_hash == record.content_hash:
                return

            # Different content with FULL_TEXT status -> reject silent overwrite of unrelated content
            raise ValueError(
                f"Conflicting full-text retrieval content for URL={record.source_url!r}: "
                f"existing_hash={existing.content_hash[:16]}..., "
                f"new_hash={record.content_hash[:16]}.... "
                "Refusing silent replacement of full-text source content."
            )

        self._records[record.evidence_id] = record
        logger.debug(
            "Evidence added: id=%s url=%s status=%s",
            record.evidence_id,
            record.source_url,
            record.retrieval_status.value,
        )

    def flush(self) -> None:
        """Atomically serialize all records to the backing JSON file.

        Writes to a temporary sibling file first and commits using os.replace()
        to guarantee atomic persistence. Prevents overwriting files belonging
        to a different run.
        """
        # Ensure parent directory exists
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        # Protect against overwriting a file belonging to a different run
        if self.filepath.exists():
            existing_run_id = None
            try:
                with self.filepath.open("r", encoding="utf-8") as fh:
                    existing_data = json.load(fh)
                if isinstance(existing_data, dict):
                    existing_run_id = existing_data.get("run_id")
            except (json.JSONDecodeError, OSError):
                # If existing file is unreadable or malformed, proceed within same run
                pass

            if existing_run_id and existing_run_id != self.run_id:
                raise FileExistsError(
                    f"Refusing to overwrite file {self.filepath} belonging to "
                    f"run_id={existing_run_id} (current run_id={self.run_id})"
                )

        payload = {
            "version": _STORE_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "record_count": len(self._records),
            "records": [r.to_dict() for r in self._records.values()],
        }
        payload_str = json.dumps(payload, indent=2, ensure_ascii=False)

        # Atomic write pattern: temporary sibling file + os.replace
        temp_file = self.filepath.with_name(
            f"{self.filepath.name}.tmp.{uuid.uuid4().hex}"
        )
        try:
            with open(temp_file, "w", encoding="utf-8") as fh:
                fh.write(payload_str)
                fh.flush()
                os.fsync(fh.fileno())

            os.replace(temp_file, self.filepath)
            logger.info(
                "Atomically flushed evidence store: %d records → %s",
                len(self._records),
                self.filepath,
            )
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Read access
    # ------------------------------------------------------------------

    def get(self, evidence_id: str) -> Optional[EvidenceRecord]:
        """Return the record for the given evidence_id, or None if absent."""
        return self._records.get(evidence_id)

    def get_by_url(self, url: str) -> Optional[EvidenceRecord]:
        """Return the best matching record for *url* (requested, final, or canonical URL).

        Precedence rules:
        1. A matching FULL_TEXT record takes precedence over SNIPPET_ONLY and FAILED records.
        2. Within the same retrieval status tier, an exact URL match is preferred
           over a canonical-only match.
        3. Ties within the same tier and exactness are broken by stable insertion order.
        """
        if not url or not isinstance(url, str):
            return None

        clean_url = url.strip()
        matching = [r for r in self._records.values() if r.matches_url(clean_url)]
        if not matching:
            return None

        def _sort_key(record: EvidenceRecord) -> Tuple[int, int]:
            if record.retrieval_status == RetrievalStatus.FULL_TEXT:
                status_tier = 3
            elif record.retrieval_status == RetrievalStatus.SNIPPET_ONLY:
                status_tier = 2
            elif record.retrieval_status == RetrievalStatus.FAILED:
                status_tier = 1
            else:
                status_tier = 0

            is_exact = 1 if (
                clean_url == record.source_url.strip()
                or (record.final_url and clean_url == record.final_url.strip())
            ) else 0

            return (status_tier, is_exact)

        return max(matching, key=_sort_key)

    def all_records(self) -> list:
        """Return all records as a list (stable insertion order)."""
        return list(self._records.values())

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[EvidenceRecord]:
        return iter(self._records.values())

    def __repr__(self) -> str:
        return (
            f"EvidenceStore(run_id={self.run_id!r}, "
            f"records={len(self._records)}, "
            f"filepath={str(self.filepath)!r})"
        )

# Stage 6B — Security Baseline Report

**Project:** Multi-Agent Research System  
**Repository:** `~/DEV/multi-agent-research-system-2`  
**Branch:** `release/stage-6-remediation`  
**Baseline Date:** 2026-11-02  
**Audit Tools:** `npm audit --json` (v12.2.1), `pip-audit` (v2.10.1 via `uvx`)  
**Runtime Environment:** Node.js v20.20.2, Python 3.13.16 (`.venv313`)

---

## 1. Executive Summary

This security baseline documents all verified vulnerabilities present in the project's dependencies prior to Stage 6B remediation. 

- **Frontend (Node.js):** 17 total vulnerabilities reported by `npm audit` (1 Critical, 2 High, 14 Moderate). The Critical and High vulnerabilities stem directly from `next@15.5.12` and its bundled transitive packages (`postcss`, `sharp`).
- **Backend (Python):** 0 vulnerabilities in `backend/requirements.txt` (FastAPI, Uvicorn, psycopg, python-dotenv).
- **Research Crew (Python):** 5 vulnerabilities reported by `pip-audit` in `chromadb 1.5.9`, an indirect transitive dependency pulled in by `crewai>=0.76.0`.
- **Repository Hygiene:** `frontend/tsconfig.tsbuildinfo` was identified as an accidentally tracked build artifact in version control.

---

## 2. Frontend Security Baseline (`npm audit`)

### 2.1 Next.js (`next@15.5.12`)
- **Dependency Type:** Direct runtime dependency (`dependencies` in `frontend/package.json`)
- **Installed Version:** `15.5.12`
- **Maximum Severity:** **Critical**
- **Fix Available:** `15.5.28` (SemVer minor/patch bump within Next.js 15.5)

#### Key Vulnerabilities & Advisories:
| Advisory ID | Severity | Title | Affected Range | Patched Version |
|:---|:---:|:---|:---|:---:|
| **GHSA-2464-fhr4-w9xw** | **Critical** | Next.js: Unauthenticated Remote Code Execution in Image Optimization API when SVG files are used | `>=10.0.0 <=15.5.27` | `15.5.28` |
| **GHSA-2xp9-vwfh-vxw4** | **Critical** | Next.js: Unauthenticated Remote Code Execution in Image Optimization API when AVIF files are used | `>=10.0.0 <15.5.24` | `15.5.24` |
| **GHSA-p293-qw3h-jr36** | **Critical** | Next.js: Unauthenticated Remote Code Execution on windows-hosted servers | `>=13.4.0 <15.5.24` | `15.5.24` |
| **GHSA-ggv3-7p47-pfv8** | **High** | Next.js: HTTP request smuggling in rewrites | `>=9.5.0 <15.5.13` | `15.5.13` |
| **GHSA-p3m5-59mp-jv5r** | **High** | Next.js: Denial of Service in App Router using Server Actions | `>=13.0.0 <=15.5.27` | `15.5.28` |
| **GHSA-26hh-7cqf-hhc6** | **High** | Next.js: Middleware / Proxy bypass in App Router applications via segment-prefetch routes | `>=15.2.0 <15.5.18` | `15.5.18` |
| **GHSA-89xv-2m56-2m9x** | **High** | Next.js: Server-Side Request Forgery in Server Actions on custom servers | `>=14.1.1 <15.5.21` | `15.5.21` |
| **GHSA-p9j2-gv94-2wf4** | **High** | Next.js: Server-Side Request Forgery in rewrites via attacker-controlled destination hostname | `>=12.0.0 <15.5.21` | `15.5.21` |
| **GHSA-c4j6-fc7j-m34r** | **High** | Next.js: SSRF in applications using WebSocket upgrades | `>=13.4.13 <15.5.16` | `15.5.16` |
| **GHSA-492v-c6pp-mqqv** | **High** | Next.js: Middleware / Proxy bypass through dynamic route parameter injection | `>=15.4.0 <15.5.16` | `15.5.16` |
| **GHSA-mg66-mrh9-m8jx** | **High** | Next.js: Denial of Service via connection exhaustion in applications using Cache Components | `>=15.0.0 <15.5.16` | `15.5.16` |
| **GHSA-q4gf-8mx6-v5v3** | **High** | Next.js: Denial of Service with Server Components | `>=13.0.0 <15.5.15` | `15.5.15` |
| **GHSA-8h8q-6873-q5fj** | **High** | Next.js: Denial of Service with Server Components | `>=13.0.0 <15.5.16` | `15.5.16` |
| **GHSA-3x4c-7xq6-9pq8** | **Moderate** | Next.js: Unbounded next/image disk cache growth can exhaust storage | `>=10.0.0 <15.5.14` | `15.5.14` |
| **GHSA-m99w-x7hq-7vfj** | **Moderate** | Next.js: Denial of Service in App Router using Server Actions | `>=13.0.0 <15.5.21` | `15.5.21` |
| **GHSA-68g3-v927-f742** | **Moderate** | Next.js: Cache confusion of response bodies for requests with bodies | `>=13.0.0 <15.5.21` | `15.5.21` |
| **GHSA-4633-3j49-mh5q** | **Moderate** | Next.js: Cache confusion for requests with invalid UTF-8 byte sequences | `>=13.0.0 <15.5.21` | `15.5.21` |
| **GHSA-4c39-4ccg-62r3** | **Moderate** | Next.js: Unbounded Server Action payload in Edge runtime | `>=13.0.0 <15.5.21` | `15.5.21` |
| **GHSA-q8wf-6r8g-63ch** | **Moderate** | Next.js: Denial of Service in Image Optimization API using SVGs | `>=15.5.0 <15.5.21` | `15.5.21` |
| **GHSA-955p-x3mx-jcvp** | **Moderate** | Next.js: Unauthenticated disclosure of internal Server Function endpoints | `>=13.0.0 <15.5.21` | `15.5.21` |
| **GHSA-4jqv-mc3x-m676** | **Moderate** | Next.js: Cache poisoning of SSG and ISR pages in self-hosted applications | `>=15.0.0 <15.5.27` | `15.5.27` |
| **GHSA-mcj8-r9mp-w47p** | **Moderate** | Next.js: Cache poisoning in SSG/ISR rendering | `>=15.0.0 <15.5.27` | `15.5.27` |
| **GHSA-ffhc-5mcf-pf4q** | **Moderate** | Next.js: Cross-site scripting in App Router applications using CSP nonces | `>=13.4.0 <15.5.16` | `15.5.16` |
| **GHSA-gx5p-jg67-6x7h** | **Moderate** | Next.js: Cross-site scripting in beforeInteractive scripts | `>=13.0.0 <15.5.16` | `15.5.16` |
| **GHSA-wfc6-r584-vfw7** | **Moderate** | Next.js: Cache poisoning in React Server Component responses | `>=14.2.0 <15.5.16` | `15.5.16` |
| **GHSA-3g8h-86w9-wvmq** | **Low** | Next.js: Middleware / Proxy redirects can be cache-poisoned | `>=12.2.0 <15.5.16` | `15.5.16` |
| **GHSA-vfv6-92ff-j949** | **Low** | Next.js: Cache poisoning via collisions in RSC cache-busting | `>=13.4.6 <15.5.16` | `15.5.16` |

---

### 2.2 PostCSS (`postcss<=8.5.22`)
- **Dependency Type:** Transitive dependency (bundled inside `next`)
- **Installed Version:** `8.5.22`
- **Severity:** **High**
- **Fix Available:** Via `next@15.5.28`

| Advisory ID | Severity | Title | Affected Range | Patched Version |
|:---|:---:|:---|:---|:---:|
| **GHSA-6g55-p6wh-862q** | **High** | PostCSS: Arbitrary file read and information disclosure via attacker-controlled sourceMappingURL | `<=8.5.11` | `8.5.12` |
| **GHSA-r28c-9q8g-f849** | **High** | PostCSS: Path Traversal in Previous Source Map Auto-Loading leads to Arbitrary .map File Disclosure | `<=8.5.17` | `8.5.18` |
| **GHSA-fxqj-rqcc-2cmp** | **Moderate** | PostCSS: Incomplete fix of GHSA-6g55-p6wh-862q | `<=8.5.22` | `8.5.23` |
| **GHSA-qx2v-qp2m-jg93** | **Moderate** | PostCSS: XSS via Unescaped `</style>` in CSS Stringify Output | `<8.5.10` | `8.5.10` |

---

### 2.3 Sharp (`sharp<=0.35.5-rc.1`)
- **Dependency Type:** Transitive dependency (bundled inside `next`)
- **Installed Version:** Bundled with Next.js image optimizer
- **Severity:** **High**
- **Fix Available:** Via `next@15.5.28`

| Advisory ID | Severity | Title | Affected Range | Patched Version |
|:---|:---:|:---|:---|:---:|
| **GHSA-f88m-g3jw-g9cj** | **High** | sharp inherited vulnerabilities in libvips (CVE-2026-33327, CVE-2026-33328, CVE-2026-35590) | `<0.35.0` | `0.35.0` |
| **GHSA-rgj7-g3m4-5g8c** | **High** | sharp vulnerabilities in libheif (GHSA-g89c-p67h-r497, GHSA-2jg2-4ch7-h545) | `<0.35.4` | `0.35.4` |
| **GHSA-wq5f-xc86-pv6w** | **High** | sharp vulnerability in librsvg dependency (CVE-2026-96889) | `<0.35.5` | `0.35.5` |

---

### 2.4 ESLint & Minimatch Toolchain
- **Dependency Type:** Build-time `devDependencies` (never bundled into production runtime)
- **Vulnerability:** `minimatch` ReDoS / DoS via globparts amplification (`<11.0.1` -> GHSA-g3hv-75wg-m249, Moderate).
- **Impact Analysis:** Development and build-time only (`npm run lint`). Not reachable in production HTTP server or client bundle.

---

## 3. Python Security Baseline (`pip-audit`)

### 3.1 Backend Tier (`backend/requirements.txt`)
- **Vulnerabilities Found:** **0**
- All backend packages (`fastapi>=0.145.0`, `uvicorn>=0.54.0`, `psycopg[binary]>=3.2.0`, `python-dotenv>=1.0.0`) are clean and have no known vulnerabilities.

### 3.2 Research Engine Tier (`research_crew/requirements.txt`)
- **Vulnerable Package:** `chromadb`
- **Installed Version:** `1.5.9` (latest release available on PyPI)
- **Dependency Type:** Indirect transitive dependency (pulled in by `crewai>=0.76.0`)
- **Fix Versions Available on PyPI:** **None** (`Fix versions: []`)

| Advisory ID | Severity | Vulnerability Summary | Upstream Fix Status |
|:---|:---:|:---|:---:|
| **PYSEC-2026-311** | High | Pre-authentication code injection in ChromaDB Python client | No upstream patch released (`Fix versions: []`) |
| **PYSEC-2026-3814** | High | Code injection in ChromaDB Python client | No upstream patch released (`Fix versions: []`) |
| **PYSEC-2026-3815** | Medium | Authorization bypass in SimpleRBACAuthorizationProvider | No upstream patch released (`Fix versions: []`) |
| **PYSEC-2026-3813** | Medium | Lack of authorization validation in ChromaDB | No upstream patch released (`Fix versions: []`) |

#### Threat & Impact Assessment for ChromaDB:
- ChromaDB is an optional vector database dependency installed transitively by `crewai`.
- In this architecture, the research engine explicitly executes with `memory=False` on the Crew configuration.
- The pipeline does not run a standalone ChromaDB server, does not expose ChromaDB over network sockets, and does not accept untrusted serialized objects into vector memory.
- **Risk Characterization:** **Accepted Risk / Inactive Vector**. No upstream fix exists, and the vulnerable code paths are never invoked.

---

## 4. Tracked Build Artifacts Baseline

- `frontend/tsconfig.tsbuildinfo`: 100,387-byte TypeScript incremental build cache accidentally committed in initial commit `08fe813`.
- **Remediation Required:** Remove from Git tracking (`git rm --cached`) and add `*.tsbuildinfo` to `.gitignore`.

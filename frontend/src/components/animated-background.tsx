import type { CSSProperties } from "react";

const PARTICLES = [
  { left: "6%", top: "18%", size: 1.5, duration: "48s", delay: "0s", opacity: 0.35 },
  { left: "14%", top: "72%", size: 1, duration: "56s", delay: "-8s", opacity: 0.28 },
  { left: "22%", top: "34%", size: 2, duration: "62s", delay: "-14s", opacity: 0.22 },
  { left: "31%", top: "88%", size: 1, duration: "44s", delay: "-3s", opacity: 0.3 },
  { left: "38%", top: "12%", size: 1.5, duration: "70s", delay: "-22s", opacity: 0.25 },
  { left: "47%", top: "58%", size: 1, duration: "52s", delay: "-11s", opacity: 0.32 },
  { left: "55%", top: "26%", size: 1.5, duration: "64s", delay: "-19s", opacity: 0.2 },
  { left: "63%", top: "81%", size: 1, duration: "47s", delay: "-6s", opacity: 0.27 },
  { left: "71%", top: "41%", size: 2, duration: "58s", delay: "-27s", opacity: 0.18 },
  { left: "78%", top: "9%", size: 1, duration: "51s", delay: "-16s", opacity: 0.3 },
  { left: "84%", top: "64%", size: 1.5, duration: "66s", delay: "-9s", opacity: 0.24 },
  { left: "91%", top: "29%", size: 1, duration: "49s", delay: "-21s", opacity: 0.28 },
  { left: "9%", top: "49%", size: 1, duration: "73s", delay: "-31s", opacity: 0.2 },
  { left: "27%", top: "6%", size: 1.5, duration: "55s", delay: "-13s", opacity: 0.26 },
  { left: "42%", top: "77%", size: 1, duration: "60s", delay: "-4s", opacity: 0.22 },
  { left: "59%", top: "15%", size: 1, duration: "68s", delay: "-25s", opacity: 0.3 },
  { left: "68%", top: "53%", size: 1.5, duration: "43s", delay: "-7s", opacity: 0.21 },
  { left: "88%", top: "86%", size: 1, duration: "57s", delay: "-18s", opacity: 0.27 },
];

export function AnimatedBackground() {
  return (
    <div className="pointer-events-none fixed inset-0 -z-10 overflow-hidden bg-[#050505]" aria-hidden="true">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_20%_0%,rgba(210,210,220,0.045),transparent_55%),radial-gradient(ellipse_60%_40%_at_85%_15%,rgba(180,180,195,0.03),transparent_50%)] motion-safe:animate-[ambient-pulse_28s_ease-in-out_infinite_alternate]" />
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-zinc-300/20 to-transparent motion-safe:animate-[sweep_22s_ease-in-out_infinite]" />
      {PARTICLES.map((particle, index) => (
        <span
          key={index}
          className="absolute rounded-full bg-zinc-200/70 motion-safe:animate-[particle-drift_linear_infinite]"
          style={
            {
              left: particle.left,
              top: particle.top,
              width: particle.size,
              height: particle.size,
              opacity: particle.opacity,
              animationDuration: particle.duration,
              animationDelay: particle.delay,
              boxShadow: "0 0 6px rgba(228,228,231,0.35)",
            } as CSSProperties
          }
        />
      ))}
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,transparent_0%,rgba(5,5,5,0.55)_75%,#050505_100%)]" />
    </div>
  );
}

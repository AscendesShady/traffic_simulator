# Regime proposal for the paper campaign (2026-09-25/26) -- not adopted

> **Not adopted.** The reported campaign (9afdd49d, 2026-09-26/27) did not
> run these regimes. It ran the defaults -- speed scale 0.5, a 100 m zone,
> a 600 s warm-up, demand 24 / 22 / 14 / 13 / 12 / 15 veh/min -- with the
> R3 / R4 headways set to 45 / 90 s (240 buses/h), seeds 567 and 876. The
> methodology (sec. 12) describes what it ran. This record stays as the
> evidence for the prompt replay, the one-lever sweep and the warm-up
> finding, which the reported campaign's own runs repeat (MSER-5 15–25 min).

The proposal, and the evidence behind each choice: a replay of the LLM
decisions against the prompt's own tests, a one-lever headless sweep, and a
pilot of the two proposed regimes.

## Why the LLM arms under-performed

Every arm's per-route decisions in campaigns 3e9df990 (seed 788) and
67b47f25 (seed 987) were scored against the assisted prompt's two tests on
the exact minimap the arm saw (AI Decision Audit sheet). Seed 987, bus-turns
where the test says grant:

| Arm | DBL: test says grant | granted | TSP: test says grant | granted |
|---|---|---|---|---|
| rule-based | 240 | 240 | 12 | 6 |
| grok-4 | 213 | 135 | 5 | 4 |
| gemini-3.5-flash-lite | 230 | 1 | 7 | 8 |
| phi3:3.8b | 245 | 3 | 9 | 31 |
| llama3.2:3b | 254 | 9 | 4 | 48 |
| llama3.1:8b | 236 | 9 | 6 | 204 |
| llama3:latest | 228 | 6 | 6 | 206 |

DBL was the lever that paid (rule-based −17 %, passenger-pressure −22 % on
seed 987), and every LLM but grok-4 skipped it. Gemini weighed DBL against
`cross_pax`: the prompt's objective sentence applied the passenger comparison
to every grant, its one worked example granted TSP only, and the reason field
named only that comparison.

## The prompt fix, replayed

The prompt now states DBL as a separate test (not weighed against
`cross_pax`, `would_stop`, `actionable` or the one-per-node cap; applied on
every route whose lane is clear), grants a DBL in its example, names both
tests in the reason, and asks for a one-pass answer (DECIDE FAST). Replayed
on 40 real bus-turn minimaps from seed 987 (57 DBL-qualifying routes), old
prompt against new, alternating per case:

| Model | DBL agreement | TSP agreement | Latency p50 / p95 (new) |
|---|---|---|---|
| gemini-3.5-flash | 87 → 100 % | 100 % | 5.9 / 8.1 s (was 8.6 / 15.9) |
| grok-4 | 76 → 99 % | 100 % | 5.1 / 6.6 s (was 8.6 / 12.1) |
| gemini-3.5-flash-lite | 49 → 78 % | 94 % | 1.4 / 1.8 s |
| llama3.1:8b | 50 → 57 % | 54 % | 1.7 / 2.1 s |
| llama3.2:3b | 48 → 59 % | 76 % | 1.2 / 1.6 s |
| llama3:latest | 49 → 44 % | 64 % | 1.6 / 2.1 s |
| phi3:3.8b | 45 → 43 % | 93 % | 1.5 / 2.1 s |

gemini-2.5-flash is retired (HTTP 404). gemini-3.8-flash answered 27 of 80
calls (all correct) but three ran past the 30 s timeout and the abandoned
requests then held the provider lock.

## One-lever sweep

From the defaults of 2026-09-25 (demand EB 24 / WB 22 / A_NB 14 / A_SB 13 /
B_NB 12 / B_SB 15 veh/min, headways 60/120/180/150/120 s, speed scale 0.5,
100 m zone), one lever changed at a time; 30-minute runs (600 s warm-up),
seeds 234 and 987, paired against the baseline of the same setting and seed.
Arms: rule-based (the prompt's two tests) and greedy (TSP whenever the bus
would stop, cross street ignored, no DBL -- the local models' failure mode).
Person-hours saved, primary DV (seed 234 / 987):

| Setting | Regime | Baseline | rule-based | greedy |
|---|---|---|---|---|
| speed 0.767 (~50 km/h) | v/c 0.79, 66 s | 275 / 340 | +47 / +65 | +25 / +52 |
| zone 200 m | v/c 0.90, 87 s | 368 / 408 | +40 / +66 | +27 / +61 |
| base | v/c 0.90, 87 s | 368 / 408 | −16 / +26 | +49 / +46 |
| demand ×1.35 | v/c 1.11, 150 s capped, 9 % latent | 994 / 927 | +53 / −4 | +15 / −11 |
| old headways (77 buses/h) | v/c 0.90 | 209 / 209 | +2 / +10 | −24 / +6 |

- Bus frequency is the precondition: at 77 buses/h no arm moves.
- A higher speed (more capacity, slack for the cross street) and a longer
  zone (DBL clears the lane earlier) turn the rule from inconsistent to
  consistently positive, and it then beats greedy.
- Oversaturation fails the validity checks and flips signs between seeds.
- One arm's seed-to-seed spread is 20–40 pax-h: only consistent gains above
  about 40 pax-h are resolvable with two seeds.

## The regimes

`control_panel.REGIMES`, applied by `parallel_campaign --regime` and by
`TRAFFIC_REGIME` for the windowed phase:

| | moderate | near-capacity |
|---|---|---|
| Demand (veh/min) | 24 / 22 / 14 / 13 / 12 / 15 | 30 / 28 / 18 / 16 / 15 / 19 (×1.25) |
| v/c, Y, Webster cycle | 0.79, 0.61, 66 s | 0.89, 0.77, 113 s |
| Mean car desired speed | 50 km/h (scale 0.772) | same |
| Priority zone | 200 m | same |
| Buses | 60/120/180/150/120 s, R6 off (164/h) | same |

Both keep Y ≤ 0.8 and an uncapped Webster cycle, the operating range the
validation note asks of an arm comparison; they differ only in demand.

## Pilot

Both regimes, the three non-LLM arms, seeds 234 and 987, 60-minute runs
through `parallel_campaign --regime` (worker mode, results kept out of the
campaign dataset). Every run passed the calibration checks: latent demand
0.1–3.8 % (< 5 %), in-network saturation flow 0.91–0.97 of calibrated
(> 0.90), GEH < 5 on 100 % of sources (83 % on one near-capacity run). Two
near-capacity runs on seed 987 did not meet `converged`.

**Warm-up.** MSER-5 on the per-minute vehicles in the network ended the fill
at 15–25 min in all twelve runs (20 min in eight), not the 10 min measured on
campaign 3e9df990: the bus-heavy regimes fill more slowly. Both regimes pin a
25-minute warm-up (`warmup_discard_frames` 90,000), leaving a 35-minute
measured window in a 60-minute run.

**Effects.** Person-hours saved against the baseline of the same seed, over
minutes 30–60 (the 60-minute steady value minus the 30-minute one, both taken
after the same warm-up snapshot, so the fill is excluded):

| Regime | Baseline (234 / 987) | rule-based | passenger-pressure-tsp |
|---|---|---|---|
| moderate | 495 / 843 | −27 / +59 | +93 / +50 |
| near-capacity | 1,262 / 1,413 | +137 / −68 | +241 / +162 |

The passenger-pressure heuristic is positive on both seeds in both regimes;
near capacity it trades car delay (−135 / −194 pax-h) for bus delay
(+376 / +356). The rule arm -- the prompt's own tests, which gemini-3.5-flash
and grok-4 now reproduce -- changes sign between seeds in both regimes, over
the full steady window and over minutes 30–60 alike, so its variance is real
and not a warm-up artefact: the same seed pair's baselines differ by 68 %
(bus trips held at the source: 28 against 47 in the moderate regime).

## Proposed campaign design (not run)

- Regimes `moderate` and `near-capacity`, each its own campaign (pairing is
  within a campaign, one baseline per seed).
- 60-minute runs, 25-minute warm-up (35-minute measured window), 10 s
  decision tick.
- Eight fresh seeds per regime, 101–108 (the pilot's 234 and 987 chose the
  regimes and are kept out of the result). With two seeds per arm the pilot
  cannot estimate the replication count reliably; `print_campaign_summary`
  states it from the campaign itself and flags an under-replicated arm.
- Phase one, headless: baseline, rule-based, passenger-pressure-tsp.
- Phase two, windowed: gemini-3.5-flash and grok-4 (the two models that follow
  the prompt's tests); gemini-3.5-flash-lite and llama3.1:8b are optional
  contrasts, reported as such.

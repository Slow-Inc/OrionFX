# Pre-registered decision rule (written 2026-11-27, before any v2 result was scored)

Scored on non-disputed cases (both labelers agree); disputed cases reported separately.
"System" answer = what M03 publishes (assess_news conflict rule applied).

Classes per call: correct | harmful (valid, UP<->DOWN flip vs truth, or directional answer on a
NEUTRAL-truth case above its confidence cap) | miss (NEUTRAL when truth is directional, or
over-confident on a capped case) | safe-degrade (invalid JSON / failed call).

1. Exclude a model if: valid rate < 97%, or harmful rate > 3%, or (end-to-end) DEGRADED rate > 5%
   under the production 20 s timeout + one retry.
2. Among the rest, take the model with the highest system accuracy. If a cheaper model is not
   significantly worse (paired bootstrap, 95% CI of the accuracy difference includes 0), take the
   cheapest such model.
3. Its projected monthly allowance (measured $/call x 200 calls/day x 30) must fit the Go plan;
   otherwise take the next model.
End-to-end entry scenarios (E1/E2) are model-independent under current M05 voting and are reported
as an M05 policy finding, not a model score; M1/M2/N (exits) and DEGRADED rate are model scores.

# Channel Analytics Roadmap

## Objective

Turn the existing comment and engagement data into channel-specific guidance
that helps creators decide what to publish, when to publish it, and where to
spend community-management time. Metrics must be compared within the same
channel and platform before any portfolio-level recommendation is made.

## Phase 1 — Data foundation and early signals

- Preserve an immutable engagement snapshot after every successful stats fetch.
- Retain raw snapshots for 90 days so the SQLite database remains bounded.
- Keep YouTube, Facebook, and Instagram collection on a 30-minute cadence.
- Rank current content within each channel/platform using a transparent
  interaction score and show an explicitly labelled confidence level.
- Never present current totals as historical growth.

Success criteria: snapshots survive later refreshes, retention is tested, and
Insights displays a useful recommendation without another paid API.

## Phase 2 — Momentum and channel baselines

- Calculate 24-hour and 7-day metric deltas from historical snapshots.
- Normalize each content item against its own channel/platform median using
  robust percentiles rather than comparing raw totals across platforms.
- Add momentum, engagement-rate, and audience-demand components to a documented
  creator-focus score.
- Require a minimum sample before showing medium/high-confidence guidance.

Success criteria: recommendations explain the period, baseline, sample size,
and factors that caused the ranking.

## Phase 3 — Audience and publishing intelligence

- Measure comment arrival patterns by weekday and hour in IST.
- Categorize comments into praise, question, request, complaint, and spam using
  deterministic rules first, with cached Gemini classification as an opt-in.
- Extract recurring topics from titles and comments per channel.
- Recommend response priorities and publishing windows.

Success criteria: creators receive actionable time/topic suggestions with an
auditable source sample and controlled AI cost.

## Phase 4 — Experiments and outcomes

- Let creators record a recommendation as accepted, ignored, or completed.
- Compare subsequent content against the pre-recommendation channel baseline.
- Add lightweight A/B tracking for title, topic, format, and publishing window.
- Report uncertainty; do not claim causation from observational data alone.

Success criteria: the dashboard learns which recommendations help each channel
without overstating small or noisy samples.

## Data governance

- Keep raw comment text access restricted to authenticated operators.
- Do not send comment text to an external model unless explicitly enabled.
- Retain only the minimum history needed for analysis and document deletion.
- Prefer aggregate features over personally identifying author-level profiles.

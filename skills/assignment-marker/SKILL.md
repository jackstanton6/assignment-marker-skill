---
name: assignment-marker
description: Mark and critique a university assignment (essay, report, lab report, code, maths, presentation) against its specification using six isolated reviewer subagents, then give an honest, calibrated mark out of 100, ranked weaknesses with fixes, strengths to protect, a path to an optional target mark, and a markdown report. Use when the user asks to mark, grade, assess, critique, review or improve an assignment, or to re-mark a revised version.
argument-hint: <spec file or pasted text> <assignment file/folder> [target mark]
---

# Assignment Marker

Six independent reviewers read the assignment against its specification. You (the main agent) prepare the inputs, run the reviewers in parallel, verify their claims, merge them into one honest verdict, and write the report.

## Principles (apply to every step)

1. **Honesty over comfort.** Report the mark the work earns. If it is strong, say so plainly and keep the issue list short. If it is weak, say so plainly in the first line. Never invent weaknesses to fill a list or praise to soften one.
2. **Isolation.** Each reviewer sees only the specification, the assignment, and its own role instructions. Never pass a reviewer the target mark, another reviewer's output, a previous report, the user's comments about the work, or your own view. The target mark is withheld so it cannot anchor anyone's marking.
3. **Evidence.** Every finding in the report points to a location that exists and a fragment that really appears there. Unverifiable findings are dropped.
4. **Mark impact first.** Rank everything by how many marks it costs, not by how strongly a reviewer worded it.
5. **Guidance, not ghostwriting.** Fixes say what to change and why. At most one short illustrative example per fix (one sentence, or three lines of code or working). Never rewrite whole paragraphs, functions or solutions.

## Inputs

| Input | Required | Notes |
|---|---|---|
| Specification | Yes | File(s) or pasted text. Include the rubric/marking criteria if the user has them; ask once if a rubric is mentioned but not given. |
| Assignment | Yes | A file, several files, or a folder (code projects). |
| Target mark | No | 0–100. Used only in synthesis. |

Inputs may arrive as `$ARGUMENTS` or in conversation. If the specification or assignment is missing, ask for it once. Never mark without a specification and never invent one. If the user says it has none, mark against the defaults for the assignment type and state that clearly in the report.

## Step 1 — Prepare the inputs

1. Make a work directory in your scratchpad (or `mktemp -d`) and remember its path as `WORK`.
2. If the specification was pasted, save it as `WORK/spec_pasted.md`.
3. Run (the skill directory is the base directory shown when this skill loaded):

   ```
   python3 "<skill_dir>/scripts/prepare.py" --spec <spec files> --assignment <assignment files or folder> --work "WORK" --cwd "$PWD"
   ```
   Add `--type <essay|report|lab|code|maths|presentation>` if the type is obvious and the guess might be wrong. If a library is missing, install it (`pip install python-docx python-pptx pypdf`) and rerun.

4. Read the JSON it prints. It gives `spec_path`, `assignment_path`, `detected_type`, `word_count`, `spec_fingerprint`, `previous_reports`, `media` and `warnings`.
5. **Confirm the type** by reading the spec. If the assignment is mixed (e.g. a report with code), pick the primary type and note the secondary one, e.g. `report (+code)`. If you change the type to or from `maths`, rerun with `--type` so the numbering matches (maths is numbered line by line).
6. **Handle warnings:**
   - *Scanned or low-text PDF, or image files (handwritten maths):* view the pages with Read and transcribe them into `assignment.md` yourself, keeping the numbering scheme (`[L#]` for maths, `[P#]` for prose) and writing maths in LaTeX. Add `Transcribed from images: yes` to the header. Transcribe exactly, including errors. Do not tidy or correct anything.
   - *Figures that carry marks* (lab reports, data reports): view the images listed in `media` and add a neutral factual line under the matching location, e.g. `[Fig 2 — described: line graph, x "Time (s)", y "Temp" with no units, 6 points, no error bars, no legend]`. Describe what is there and what is missing. Do not evaluate.
   - *Very large input* (over ~15,000 words, or the code cap was hit): tell the user the cost and ask which parts matter before continuing.
7. Skim `assignment.md` once to check the numbering looks right. Do not form a view of the mark here.

## Step 2 — Execution evidence (code only)

Marking code without running it is guesswork, so for code assignments:

1. Copy the project into `WORK/run/` so the student's files are never modified.
2. Find the intended run or test command (spec, README, Makefile, `tests/`, `package.json`, etc.). Run it once with a timeout of about 120 seconds.
3. Do not install new dependencies, use the network, or run anything that deletes files outside the project or needs credentials without asking the user first.
4. Append to `assignment.md`:

   ```
   ## Execution evidence (collected by the marking tool)
   Command: <command>
   Exit code: <n>
   Output (first 60 lines):
   <output>
   ```
   If it could not run, append `Not executed: <reason>`.

This section is a factual record of the assignment's behaviour, not commentary, so it does not break isolation.

## Step 3 — Run the six reviewers in parallel

Launch all six in **one message** with the Agent tool (`general-purpose` type).

| Reviewer | Role file | Model | Word limit | Gives a mark |
|---|---|---|---|---|
| Marker | `agents/marker.md` | inherit (omit `model`) | 250 | Yes (rubric-based) |
| PhD professor | `agents/phd.md` | inherit | 150 | Yes |
| Tutor | `agents/tutor.md` | `sonnet` | 150 | No |
| English teacher | `agents/english.md` | `haiku` | 150 | No |
| Strength finder | `agents/strengths.md` | `sonnet` | 150 | No |
| Rival student | `agents/rival.md` | inherit | 150 | Yes |

Use this prompt for each, filling the placeholders and adding nothing else:

```
You are one of several independent reviewers of a university assignment.
Read exactly these files and nothing else:
1. <skill_dir>/agents/_shared.md
2. <skill_dir>/agents/<role>.md
3. <skill_dir>/agents/_calibration.md      <- marker, phd and rival only; omit this line for the others
4. <WORK>/spec.md
5. <WORK>/assignment.md
Assignment type: <type>
Do not read other files, search, browse the web, or run commands.
Follow your role file. Reply with only its output format, in at most <N> words.
```

If a reviewer fails or returns something unusable, rerun that reviewer once. Accept a reply that is slightly over the word limit rather than paying for a rerun.

## Step 4 — Verify

Do these before merging anything.

1. **Citation check.** For every finding, confirm the location exists in `assignment.md` and the quoted fragment appears there (use Grep). If the fragment exists at another location, correct the location. If it does not exist anywhere, drop the finding.
2. **Arithmetic.** If the marker scored weighted criteria, recompute the total with Python and use the computed value. Check the reported word count against the header figure.
3. **Contested points.** Verify only items that are both
   (a) tagged `[VERIFY]`, or disputed between reviewers (one praises what another calls wrong), **and**
   (b) likely to make the top five weaknesses or move the mark by 2 or more.

   Use the cheapest method that settles it:
   - Careful reading and reasoning from the text.
   - Execution: for maths, check derivations and final answers with Python/SymPy or a numerical check; for code, write a small targeted test in `WORK/run/` to confirm or refute a claimed bug.
   - Web search, last resort: **no more than 3 searches per marking run**, only for factual or citation claims central to the argument. Record the source.

   Record each result as confirmed, refuted or unresolved. Refuted findings are dropped. Unresolved ones are reported as "possible", never as fact.

## Step 5 — Synthesise

### Merge findings
- Group findings that describe the same problem at the same place (or the same recurring pattern).
- Confidence comes from independent agreement: a problem flagged by 3 or more reviewers is high-confidence. A problem flagged by one reviewer is kept only if it was verified or is plainly correct on reading; otherwise drop it or list it under minor issues as "possible".
- Classify severity:
  - **Critical:** fails or omits a spec requirement; a factual, mathematical or logical error the argument or result depends on; code that does not run or fails a core requirement.
  - **Major:** clearly costs marks on a criterion (shallow analysis, unsupported key claim, missing edge cases, unjustified proof step, weak structure).
  - **Minor:** polish (isolated grammar, formatting, small style issues).
- Estimate each weakness's mark cost from the weight of the criterion it hits and its severity. Rank by that.
- **Conflicts:** when reviewers disagree, say so in one line and give your verdict with the reason.
- **Strengths:** take them from the strength finder and any positives elsewhere, keeping only those tied to a location and a criterion.

### Compute the mark
1. Take the three marks: marker (M), PhD (P), rival (R).
2. **Base mark = median of M, P and R.**
3. Adjust only for verification results: if verification refuted a problem the reviewers deducted for, or confirmed a serious error some reviewers missed, move the mark by an amount consistent with the affected criterion's weight, and state the adjustment in the report.
4. Do not go above the highest or below the lowest of the three marks unless verification gives a stated reason.
5. Apply penalties the spec states (e.g. word-count penalties). If the spec states none, flag the risk instead of inventing a penalty.
6. Spread = highest − lowest: 0–6 high confidence, 7–12 moderate, over 12 low. With low confidence, explain what the reviewers disagreed about.
7. Convert to a band using the spec's scale if it gives one; otherwise use `agents/_calibration.md`.
8. **You may not raise the mark to be kind.** If your own reading suggests a different mark from the median, explain why in the report rather than quietly changing it.

### Target mark (if one was given)
Only now, look at the target. Gap = target − mark.

- **0 or less:** it meets the target. Say what keeps it there and what puts it at risk.
- **1–5:** reachable through focused fixes.
- **6–12:** needs substantial revision of specific sections. Name them.
- **Over 12:** say plainly that it is unlikely without major rework, and name the few changes that matter most.

List the changes that close the gap with approximate gains. Gains are capped by each criterion's weight and must add up honestly: never say the target is reachable unless the listed gains cover it.

## Step 6 — Re-mark comparison (if there is a previous report)

`prepare.py` lists earlier `marking-report-*.md` files in the working directory with a matching `spec_fingerprint`. Use the most recent one. If the user asks for a re-mark but none matches, ask for the previous report's path or mark from scratch. If the spec has changed (different fingerprint), mark fresh and say so.

Read the previous report **only after Step 5 is finished**, so it cannot anchor this mark. Then:
- For each previous weakness (W1, W2…), find it by content rather than location (locations move between versions) and mark it **Fixed**, **Partly fixed**, **Not fixed** or **Section removed**.
- List new weaknesses introduced by the revision, and any strengths that were lost.
- Report the mark change. If |change| ≤ 3 and the two reviewer ranges overlap, say the change is within marking noise and not a meaningful improvement or decline.

## Step 7 — Write and print the report

1. Fill in `report-template.md`. Include only the sections that apply. Use at most five top weaknesses, and fewer if the work is strong. Never pad.
2. Save it to the current working directory as `marking-report-<report_stem>-<timestamp>.md`, using `report_stem` and `timestamp` from the `prepare.py` output. Never modify the student's files.
3. Print the full report in your final message so it appears in the terminal, followed by one line giving the saved path.

## Assignment-type notes

- **Essays/reports:** the argument and analysis criteria usually carry most of the marks. Polished prose does not make up for shallow analysis.
- **Lab reports:** check data, units, uncertainty and whether the conclusions follow from the results. Use the figure descriptions from Step 1.
- **Code:** correctness comes from the execution evidence first and reading second. Locations are `path:line`.
- **Maths:** every step must be valid, not just the final answer. A correct answer with no working earns little; working with one early error should lose marks at that step, with credit for correct method after it (follow-through), unless the spec says otherwise. Locations are `[L#]`.
- **Presentations:** only slides and speaker notes can be assessed, not delivery. Say so in the report.

## Failure handling

- If a reviewer's output is unusable after one rerun, continue with the rest, base the mark on the remaining markers, and set confidence to low.
- If two of the three marking reviewers fail, do not give a mark. Report the findings and say why there is no mark.
- If the assignment is clearly not an attempt at the spec (wrong file, wrong task), stop and tell the user before running reviewers.

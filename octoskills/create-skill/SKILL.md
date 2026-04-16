---
name: create-skill
description: Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, update or optimize an existing skill, run evals to test a skill, benchmark skill performance with variance analysis, or optimize a skill's description for better triggering accuracy.
version: 1.0.0
---

# Skill Creator

A skill for creating new skills and iteratively improving them.

At a high level, the process of creating a skill goes like this:

- Decide what you want the skill to do and roughly how it should do it
- Write a draft of the skill
- Create a few test prompts and simulate running them inline
- Help the user evaluate the results both qualitatively and quantitatively
  - Draft quantitative expectations, then evaluate them against the simulated outputs
  - Present results clearly in the conversation for the user to review
- Rewrite the skill based on feedback
- Repeat until satisfied
- Expand the test set and try again at larger scale

Your job when using this skill is to figure out where the user is in this process and then jump in and help them progress through these stages. So for instance, maybe they're like "I want to make a skill for X". You can help narrow down what they mean, write a draft, write the test cases, simulate/evaluate them, and repeat.

On the other hand, maybe they already have a draft of the skill. In this case you can go straight to the eval/iterate part of the loop.

Of course, you should always be flexible and if the user is like "I don't need to run a bunch of evaluations, just vibe with me", you can do that instead.

Then after the skill is done (but again, the order is flexible), you can also optimize the description, which we have guidance on below.

Cool? Cool.

## Communicating with the user

The skill creator is liable to be used by people across a wide range of familiarity with coding jargon. Pay attention to context cues to understand how to phrase your communication. In the default case:

- "evaluation" and "benchmark" are borderline, but OK
- for "JSON" and "assertion" you want to see serious cues from the user that they know what those things are before using them without explaining them

It's OK to briefly explain terms if you're in doubt.

---

## Creating a skill

### Capture Intent

Start by understanding the user's intent. The current conversation might already contain a workflow the user wants to capture (e.g., they say "turn this into a skill"). If so, extract answers from the conversation history first — the steps taken, corrections the user made, input/output formats observed. The user may need to fill the gaps, and should confirm before proceeding.

1. What should this skill enable the bot to do?
2. When should this skill trigger? (what user phrases/contexts)
3. What's the expected output format?
4. Should we set up test cases to verify the skill works? Skills with objectively verifiable outputs (file transforms, data extraction, code generation, fixed workflow steps) benefit from test cases. Skills with subjective outputs (writing style, tone) often don't need them.

### Interview and Research

Proactively ask questions about edge cases, input/output formats, example files, success criteria, and dependencies. Wait to write test prompts until you've got this part ironed out.

### Write the SKILL.md

Based on the user interview, fill in these components:

- **name**: Skill identifier (kebab-case, matches the directory name)
- **description**: When to trigger, what it does. This is the primary triggering mechanism — include both what the skill does AND specific contexts for when to use it. All "when to use" info goes here, not in the body. Note: there's a tendency to "undertrigger" skills — to not use them when they'd be useful. To combat this, make the skill descriptions a little bit "pushy". So instead of "How to build a dashboard.", write "How to build a dashboard. Make sure to use this skill whenever the user mentions dashboards, data visualization, or wants to display any kind of data, even if they don't explicitly ask for a 'dashboard.'"
- **the rest of the skill :)**

### Skill Writing Guide

#### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable Python scripts for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

Use `load_reference("skill-name", "filename.md")` to load a reference file.
Use `run_script("skill-name", "script.py", args)` to execute a bundled script.

#### Progressive Disclosure

Skills use a three-level loading system:
1. **Metadata** (name + description) — Always in context (~100 words)
2. **SKILL.md body** — In context whenever skill triggers (<500 lines ideal)
3. **Bundled resources** — As needed (scripts run without loading into context)

**Key patterns:**
- Keep SKILL.md under 500 lines; if approaching this limit, move detail to references/
- Reference files clearly from SKILL.md with guidance on when to read them
- For large reference files (>300 lines), include a table of contents

**Domain organization**: When a skill supports multiple domains/frameworks, organize by variant:
```
cloud-deploy/
├── SKILL.md (workflow + selection)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```

#### Writing Patterns

Prefer using the imperative form in instructions.

**Defining output formats:**
```markdown
## Report structure
ALWAYS use this exact template:
# [Title]
## Executive summary
## Key findings
## Recommendations
```

**Examples pattern:**
```markdown
## Commit message format
**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

### Writing Style

Try to explain to the model why things are important in lieu of heavy-handed musty MUSTs. Use theory of mind and try to make the skill general and not super-narrow to specific examples. Start by writing a draft and then look at it with fresh eyes and improve it.

### Test Cases

After writing the skill draft, come up with 2-3 realistic test prompts — the kind of thing a real user would actually say. Share them with the user: "Here are a few test cases I'd like to try. Do these look right, or do you want to add more?" Then simulate running them.

Save test cases to `evals/evals.json` within the skill directory. See `references/schemas.md` for the full schema.

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's task prompt",
      "expected_output": "Description of expected result",
      "expectations": [
        "The output includes X",
        "The response uses the skill's template"
      ]
    }
  ]
}
```

## Running and evaluating test cases

This section is one continuous sequence — don't stop partway through.

Organize results in an `<skill-name>-workspace/` directory as a sibling to the skill directory, with subdirectories per iteration (`iteration-1/`, `iteration-2/`, etc.) and per test case (`eval-0/`, `eval-1/`, etc.).

### Step 1: Run all test cases inline

For each test case, read the SKILL.md and follow its instructions to complete the task yourself. Work through them one at a time. This is less rigorous than independent runs (you wrote the skill and you're running it, so you have full context), but it's a useful sanity check.

For each run:
- Save outputs to `<workspace>/iteration-<N>/<eval-name>/outputs/`
- Write `eval_metadata.json` with the prompt and assertions (see `references/schemas.md`)
- Give each eval a descriptive name based on what it's testing — not just "eval-0"

### Step 2: Draft assertions

While working through the runs, draft quantitative assertions for each test case and explain them to the user. Good assertions are objectively verifiable and descriptively named.

Update `eval_metadata.json` files and `evals/evals.json` with the assertions once drafted.

### Step 3: Grade outputs

For each run, apply the grader process from `references/grader.md` to evaluate each assertion against the outputs. Save results to `<workspace>/iteration-<N>/<eval-name>/grading.json` using the schema in `references/schemas.md`.

### Step 4: Present results

Present the graded results directly in the conversation for each test case:
- Show the prompt and what the skill produced
- Show assertion pass/fail with evidence
- Highlight patterns: what worked, what didn't

Ask the user: "How does this look? Anything you'd change?"

### Step 5: Read the feedback

Empty feedback means the user thought it was fine. Focus improvements on cases where the user had specific complaints.

---

## Improving the skill

This is the heart of the loop. You've run the test cases, the user has reviewed the results, and now you need to make the skill better based on their feedback.

### How to think about improvements

1. **Generalize from the feedback.** We're trying to create skills that work across many different prompts. Rather than put in fiddly overfitty changes, or oppressively constrictive MUSTs, if there's some stubborn issue, try branching out and using different metaphors, or recommending different patterns of working.

2. **Keep the prompt lean.** Remove things that aren't pulling their weight. If it looks like the skill is making the bot waste time doing unproductive things, try getting rid of the parts of the skill that are causing that.

3. **Explain the why.** Try hard to explain the **why** behind everything you're asking the model to do. Even if the feedback from the user is terse or frustrated, try to actually understand the task and transmit this understanding into the instructions. If you find yourself writing ALWAYS or NEVER in all caps, that's a yellow flag — reframe and explain the reasoning instead. That's a more humane, powerful, and effective approach.

4. **Look for repeated work across test cases.** If all test cases resulted in writing similar helper scripts or taking the same multi-step approach, that's a strong signal the skill should bundle that script. Write it once, put it in `scripts/`, and tell the skill to use it.

### The iteration loop

After improving the skill:

1. Apply improvements to the skill
2. Rerun all test cases into a new `iteration-<N+1>/` directory
3. Present results to the user with a before/after comparison
4. Read feedback, improve again, repeat

Keep going until:
- The user says they're happy
- The feedback is all empty (everything looks good)
- You're not making meaningful progress

---

## Advanced: Blind comparison

For situations where you want a more rigorous comparison between two versions of a skill (e.g., "is the new version actually better?"), there's a blind comparison process. Read `references/analyzer.md` for the details. The basic idea is: evaluate two outputs against the same rubric without knowing which version produced which, then analyze why the winner won.

This is optional and most users won't need it. The human review loop is usually sufficient.

---

## Description Optimization

The description field in SKILL.md frontmatter is the primary mechanism that determines whether the bot invokes a skill. After creating or improving a skill, offer to optimize the description for better triggering accuracy.

### Step 1: Generate trigger eval queries

Create 20 eval queries — a mix of should-trigger and should-not-trigger. Save as JSON in the workspace:

```json
[
  {"query": "the user prompt", "should_trigger": true},
  {"query": "another prompt", "should_trigger": false}
]
```

The queries must be realistic. Not abstract requests, but concrete and specific with good detail. Use a mix of different lengths. Focus on edge cases rather than clear-cut cases.

Bad: `"Format this data"`, `"Create a chart"`

Good: `"ok so my boss just sent me this xlsx file and she wants me to add a column that shows the profit margin as a percentage. The revenue is in column C and costs are in column D i think"`

For **should-trigger** queries (8-10): different phrasings of the same intent — some formal, some casual. Include cases where the user doesn't explicitly name the skill but clearly needs it.

For **should-not-trigger** queries (8-10): near-misses that share keywords but actually need something different. These are the most valuable cases — naive keyword matches would trigger but shouldn't.

### Step 2: Review with user

Present the eval set to the user. They can edit queries, add/remove entries, toggle should-trigger. Get sign-off before running.

### Step 3: Manually evaluate the current description

Go through each query mentally and predict whether the current description would cause the skill to trigger. Count false positives (triggered when shouldn't) and false negatives (didn't trigger when should). This gives you a baseline.

### Step 4: Propose a new description

Based on the failures, draft an improved description. Consider:
- Adding more specific trigger phrases for false negatives
- Adding "do NOT use for X" clauses for false positives
- Making the description more or less general

Run the eval set against the new description mentally and compare scores.

### Step 5: Apply the result

Update the skill's SKILL.md frontmatter. Show the user before/after and report the improvement.

---

## Packaging

To package a skill for distribution, run the bundled packaging script:

```
run_script("create-skill", "package_skill.py", ["<path/to/skill-folder>"])
```

This creates a `.skill` zip file in the same directory as the skill folder.

---

## Reference files

- `references/schemas.md` — JSON structures for evals.json, grading.json, etc.
- `references/grader.md` — How to evaluate assertions against outputs
- `references/analyzer.md` — How to do blind A/B comparison and analyze why a version won

---

Repeating the core loop here for emphasis:

- Figure out what the skill is about
- Draft or edit the skill
- Run it yourself on test prompts
- With the user, evaluate the outputs qualitatively and quantitatively
- Repeat until you and the user are satisfied
- Package the final skill if needed.

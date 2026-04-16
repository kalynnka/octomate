# Blind Comparator and Post-hoc Analyzer

Two related processes: blind comparison (which version is better?) and post-hoc analysis (why?).

---

## Blind Comparator

Compare two outputs WITHOUT knowing which version produced them.

### Role

Judge which output better accomplishes the task. You receive two outputs labeled A and B, but you do NOT know which version produced which. This prevents bias.

### Process

#### Step 1: Read Both Outputs

Examine output A and output B. Note the type, structure, and content of each.

#### Step 2: Understand the Task

Read the eval prompt carefully. Identify what the task requires: what should be produced, what qualities matter (accuracy, completeness, format).

#### Step 3: Generate Evaluation Rubric

Based on the task, generate a rubric with two dimensions:

**Content Rubric** (what the output contains):
| Criterion | 1 (Poor) | 3 (Acceptable) | 5 (Excellent) |
|-----------|----------|----------------|---------------|
| Correctness | Major errors | Minor errors | Fully correct |
| Completeness | Missing key elements | Mostly complete | All elements present |
| Accuracy | Significant inaccuracies | Minor inaccuracies | Accurate throughout |

**Structure Rubric** (how the output is organized):
| Criterion | 1 (Poor) | 3 (Acceptable) | 5 (Excellent) |
|-----------|----------|----------------|---------------|
| Organization | Disorganized | Reasonably organized | Clear, logical structure |
| Formatting | Inconsistent/broken | Mostly consistent | Professional, polished |
| Usability | Difficult to use | Usable with effort | Easy to use |

Adapt criteria to the specific task.

#### Step 4: Evaluate Each Output

For each output:
1. Score each criterion on the rubric (1-5 scale)
2. Calculate dimension totals: Content score, Structure score
3. Calculate overall score: Average of dimension scores, scaled to 1-10

#### Step 5: Determine the Winner

Compare A and B:
1. **Primary**: Overall rubric score
2. **Tiebreaker**: If truly equal, declare a TIE

Be decisive — ties should be rare.

#### Step 6: Write Comparison Results

Save to `comparison.json` (see `schemas.md` for schema).

### Guidelines

- **Stay blind**: DO NOT try to infer which version produced which output
- **Be specific**: Cite specific examples when explaining strengths and weaknesses
- **Be decisive**: Choose a winner unless outputs are genuinely equivalent
- **Be objective**: Focus on correctness and completeness, not style preferences

---

## Post-hoc Analyzer

Analyze blind comparison results to understand WHY the winner won and generate improvement suggestions.

### Role

After the blind comparison determines a winner, "unblind" the results by examining what each version's skill said and how it affected the output. Extract actionable insights.

### Process

#### Step 1: Read the Comparison Result

Note the winning side, the reasoning, and what the comparator valued in the winning output.

#### Step 2: Compare Both Skill Versions

Read both skill versions. Identify structural differences:
- Instructions clarity and specificity
- Script/tool usage patterns
- Example coverage
- Edge case handling

#### Step 3: Compare Both Outputs

Look at execution patterns:
- How closely did each follow the skill's instructions?
- Where did the loser diverge from optimal behavior?
- Did either encounter or handle errors differently?

#### Step 4: Analyze Instruction Following

For each version's output, evaluate:
- Did the bot follow the skill's explicit instructions?
- Were there missed opportunities to leverage skill content?
- Did the bot add unnecessary steps not in the skill?

Score instruction following 1-10 and note specific issues.

#### Step 5: Identify Winner Strengths and Loser Weaknesses

Be specific. Quote from skills/outputs where relevant.

#### Step 6: Generate Improvement Suggestions

Produce actionable suggestions for improving the losing version:
- Specific instruction changes
- Scripts/tools to add or modify
- Examples to include
- Edge cases to address

Prioritize by impact. Focus on changes that would have changed the outcome.

#### Step 7: Write Analysis Results

Save to `analysis.json` (see `schemas.md` for schema).

### Guidelines

- **Be specific**: Quote from skills and outputs, don't just say "instructions were unclear"
- **Be actionable**: Suggestions should be concrete changes, not vague advice
- **Focus on skill improvements**: The goal is to improve the losing skill, not critique the bot
- **Prioritize by impact**: Which changes would most likely have changed the outcome?
- **Consider causation**: Did the skill weakness actually cause the worse output, or is it incidental?

### Categories for Suggestions

| Category | Description |
|----------|-------------|
| `instructions` | Changes to the skill's prose instructions |
| `scripts` | Scripts or utilities to add/modify |
| `examples` | Example inputs/outputs to include |
| `error_handling` | Guidance for handling failures |
| `structure` | Reorganization of skill content |
| `references` | External docs or resources to add |

---

## Analyzing Benchmark Results

When looking at multiple runs across test cases, the goal is to **surface patterns and anomalies**, not suggest skill improvements.

Look for:
- Assertions that always pass in both versions (may not differentiate skill value)
- Assertions that always fail in both versions (may be beyond capability or broken)
- High-variance results (possibly flaky eval or non-deterministic behavior)
- Patterns across eval types (certain inputs consistently harder/easier?)

Surface observations as freeform notes:
- "Assertion 'Output is well-formatted' passes 100% in both versions — may not differentiate skill value"
- "Eval 3 shows high variance — the edge case may be non-deterministic"
- "Without-skill outputs consistently fail on table extraction — skill clearly adds value here"

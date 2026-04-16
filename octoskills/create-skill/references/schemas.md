# JSON Schemas

This document defines the JSON schemas used by the create-skill skill.

---

## evals.json

Defines the evals for a skill. Located at `evals/evals.json` within the skill directory.

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's example prompt",
      "expected_output": "Description of expected result",
      "files": [],
      "expectations": [
        "The output includes X",
        "The skill used script Y"
      ]
    }
  ]
}
```

**Fields:**
- `skill_name`: Name matching the skill's frontmatter
- `evals[].id`: Unique integer identifier
- `evals[].prompt`: The task to execute
- `evals[].expected_output`: Human-readable description of success
- `evals[].files`: Optional list of input file paths (relative to skill root)
- `evals[].expectations`: List of verifiable statements about the output

---

## eval_metadata.json

Written per eval run directory. Located at `<workspace>/iteration-N/<eval-name>/eval_metadata.json`.

```json
{
  "eval_id": 1,
  "eval_name": "descriptive-name-here",
  "prompt": "The user's task prompt",
  "assertions": [
    {
      "text": "The output includes a summary section",
      "description": "Why this assertion matters"
    }
  ]
}
```

---

## grading.json

Output from grading an eval run. Located at `<workspace>/iteration-N/<eval-name>/grading.json`.

```json
{
  "expectations": [
    {
      "text": "The output includes the name 'John Smith'",
      "passed": true,
      "evidence": "Found in output: 'Extracted names: John Smith, Sarah Johnson'"
    },
    {
      "text": "The spreadsheet has a SUM formula in cell B10",
      "passed": false,
      "evidence": "No spreadsheet was created. The output was a text file."
    }
  ],
  "summary": {
    "passed": 2,
    "failed": 1,
    "total": 3,
    "pass_rate": 0.67
  },
  "claims": [
    {
      "claim": "All required fields were populated",
      "type": "quality",
      "verified": false,
      "evidence": "Reference section was left blank despite data being available"
    }
  ],
  "eval_feedback": {
    "suggestions": [
      {
        "assertion": "The output includes the name 'John Smith'",
        "reason": "A hallucinated document that mentions the name would also pass"
      }
    ],
    "overall": "Assertions check presence but not correctness."
  }
}
```

**Fields:**
- `expectations[]`: Graded expectations with `text`, `passed` (bool), `evidence` (string)
- `summary`: Aggregate pass/fail counts and `pass_rate` (0.0–1.0)
- `claims`: Implicit claims extracted from output, each with `claim`, `type`, `verified`, `evidence`
- `eval_feedback`: (optional) Improvement suggestions for the evals themselves

---

## comparison.json

Output from blind comparator. Located at `<workspace>/comparison.json`.

```json
{
  "winner": "A",
  "reasoning": "Output A is complete with proper formatting. Output B is missing the date field.",
  "rubric": {
    "A": {
      "content": {"correctness": 5, "completeness": 5, "accuracy": 4},
      "structure": {"organization": 4, "formatting": 5, "usability": 4},
      "content_score": 4.7,
      "structure_score": 4.3,
      "overall_score": 9.0
    },
    "B": {
      "content": {"correctness": 3, "completeness": 2, "accuracy": 3},
      "structure": {"organization": 3, "formatting": 2, "usability": 3},
      "content_score": 2.7,
      "structure_score": 2.7,
      "overall_score": 5.4
    }
  },
  "output_quality": {
    "A": {"score": 9, "strengths": ["Complete", "Well-formatted"], "weaknesses": []},
    "B": {"score": 5, "strengths": ["Readable"], "weaknesses": ["Missing date field"]}
  }
}
```

---

## analysis.json

Output from post-hoc analyzer. Located at `<workspace>/analysis.json`.

```json
{
  "comparison_summary": {
    "winner": "A",
    "winner_version": "iteration-2",
    "loser_version": "iteration-1",
    "comparator_reasoning": "Brief summary of why winner won"
  },
  "winner_strengths": [
    "Clear step-by-step instructions for handling multi-page documents"
  ],
  "loser_weaknesses": [
    "Vague instruction 'process appropriately' led to inconsistent behavior"
  ],
  "improvement_suggestions": [
    {
      "priority": "high",
      "category": "instructions",
      "suggestion": "Replace vague instruction with explicit steps",
      "expected_impact": "Would eliminate ambiguity"
    }
  ]
}
```

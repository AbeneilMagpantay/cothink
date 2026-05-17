# Cothink dogfood log

Copy this file to `<project_dir>/_collab/dogfood_log.md` when you start
running real cothink turns against a project. Log every turn you actually
do, not synthetic tests.

Purpose: gather empirical signal on whether the existing VSCode Workbench
feels right on real PetLet/TARA workflow — surface choice (TUI vs IDE vs
Tauri vs Cursor-polish) gets decided AFTER ≥10 turns, not before.

Scoring (1–5) is rough; the freeform `Pain:` and `Joy:` lines matter more.

---

## Turn 1 — [date] [time] — [project name]

**Task:** [one-line description of what you asked cothink to do]

**Surface:** VSCode Workbench panel (Antigravity)

**Pain (what felt clunky / wrong / missing):**
- [bullet] 

**Joy (what felt actually good):**
- [bullet]

**Cothink phases that ran:** Discovery / Planning / Executing / Mechanical /
Learnings Enforcer / Contract Review / Project State Journal

**Surface feel (1–5):** [1=fight-the-tool, 5=Cursor-grade premium]

**Integrity feel (1–5):** [1=rubber-stamped slop, 5=both brains genuinely caught stuff]

**Outcome:** shipped / aborted / rolled back via `git reset --hard <hash>`

**Notes / surprises:** [anything unexpected — token pacer behavior, screenshot paste, journal write, etc.]

---

## Turn 2 — [date] [time] — [project]

...

---

## Post-10-turns synthesis (fill in after Turn 10)

**Top 3 pain points across all turns:**
1. 
2. 
3. 

**Top 3 joy points:**
1. 
2. 
3. 

**Does the Workbench panel feel right?** [yes / no / partly]

**If "no" or "partly," which is the bottleneck?**
- [ ] Surface (need TUI / Tauri / different IDE entirely)
- [ ] Micro-interaction (need Cursor's context chips, inline diffs, etc.)
- [ ] Integrity (dual-brain rubber-stamps too much / not enough)
- [ ] Workflow fit (cothink interrupts at the wrong moments)
- [ ] Speed (too slow per turn; 4× peer-review hits real-world friction)
- [ ] Something else: _______

**Next architectural move (locked AFTER this synthesis, not before):**
- _______

---

## How to log efficiently

- Add a turn as you finish each cothink run. Don't batch — fresh memory matters.
- "Pain" and "Joy" are 1-sentence each, not paragraphs.
- If you notice the same pain across 3+ turns, mark it ⚠ — it's signal.
- If you genuinely felt "this is great," mark it ✓ — also signal.
- If 5+ consecutive turns show the same pain, stop dogfooding early and fix it. The 10-turn target is a floor, not a contract.

"""评测：用固定的任务集反复运行 LocAgent，给出可以前后对比的分数。

参考 pico 的 scripts/run_real_benchmark.py 和 Anthropic 关于 agent 评测的做法：
- 任务（task）：一个工作区初始文件（repo/）+ 一句请求（task.json 的 prompt）+ 一个判分脚本（check.py）
- 试验（trial）：同一个任务跑多次，模型每次输出不同，跑一次说明不了问题
- 判分看结果不看过程：check.py 检查运行结束后的文件和最终回答，不检查走了哪些步骤
- 判分脚本放在工作区外面，agent 看不到，不能“照着答案写”
- 每次试验都从一份干净的副本开始，互不影响
- 过程记录（transcript）就是会话文件，失败时打开它看是模型的问题还是任务、判分的问题

任务目录 evals/tasks/<任务id>/：
    task.json   prompt、max_steps、category、kind（capability 能力题 / regression 回归题），可选 allowed_tools
    repo/       工作区初始文件，每次试验复制一份
    check.py    判分脚本：在试验工作区里运行，最终回答从标准输入读入；退出码 0 为通过
                可用环境变量 LOCAGENT_TASK_DIR 找到任务目录（例如和 repo/ 里的原始文件比较）
    solution/   参考答案：覆盖到 repo/ 副本上应当能通过 check.py；其中的 answer.txt 作为最终回答

用法（在 G:\\Agent\\Harness 下）：
    uv run locagent-eval --check-solutions          只检查任务本身：原始 repo 必须不通过，参考答案必须通过（不调用模型）
    uv run locagent-eval --trials 3                  全部任务各跑 3 次
    uv run locagent-eval --trials 1 --tasks sample_replace bom_edit

结果写在 artifacts/evals/<时间>/ 下：
    <任务id>/trial_<n>/      该次试验的工作区副本，会话文件在其中的 .locagent/sessions/
    results.jsonl            每次试验一行明细
    summary.json             汇总：总体通过率、pass@k、pass^k、每个任务的平均步数和 token
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import events as ev
from .cli import _build_model_client, print_event
from .config import load_project_env
from .context_manager import DEFAULT_CONTEXT_BUDGET
from .runtime import LocAgent
from .session_store import SessionStore
from .providers.qwen import qwen_settings
from .task_state import STOP_REASON_FINAL_ANSWER, STOP_REASON_MODEL_ERROR
from .workspace import WorkspaceContext, middle

# 项目根目录（G:\Agent\Harness）：任务目录、.env 和结果目录都相对于它
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS_DIR = ROOT / "evals" / "tasks"
DEFAULT_OUT_DIR = ROOT / "artifacts" / "evals"
# 参考答案目录里作为最终回答的文件，不复制进工作区
SOLUTION_ANSWER_FILE = "answer.txt"
CHECK_TIMEOUT_SECONDS = 120
# 判分输出只保留结尾部分（断言失败信息在最后）
CHECK_OUTPUT_LIMIT = 1500
TASK_KINDS = ("capability", "regression")

# 失败类型：判分不通过；其他失败直接用停止原因（step_limit_reached、model_error 等）
FAIL_CHECK = "check_failed"


def load_tasks(tasks_dir, selected=None):
    tasks = []
    for task_dir in sorted(path for path in Path(tasks_dir).iterdir() if path.is_dir()):
        task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        if task["id"] != task_dir.name:
            raise ValueError(f"task id {task['id']} does not match directory {task_dir.name}")
        if task["kind"] not in TASK_KINDS:
            raise ValueError(f"task {task['id']}: kind must be one of {TASK_KINDS}")
        for name in ("repo", "check.py", "solution"):
            if not (task_dir / name).exists():
                raise ValueError(f"task {task['id']}: missing {name}")
        task["dir"] = task_dir
        tasks.append(task)
    if selected:
        unknown = sorted(set(selected) - {task["id"] for task in tasks})
        if unknown:
            raise ValueError(f"unknown task id: {', '.join(unknown)}")
        tasks = [task for task in tasks if task["id"] in selected]
    return tasks


def run_check(task, workspace, final_answer):
    """在工作区里运行判分脚本，返回 (是否通过, 输出结尾)。"""
    env = dict(os.environ)
    # 判分脚本在工作区外面，加上 PYTHONPATH 才能 import 工作区里的模块
    env["PYTHONPATH"] = str(workspace)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 不在工作区里留下 __pycache__，避免影响“文件是否被改动”的检查
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LOCAGENT_TASK_DIR"] = str(task["dir"])
    try:
        completed = subprocess.run(
            [sys.executable, str(task["dir"] / "check.py")],
            cwd=workspace,
            input=final_answer,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"check timed out after {CHECK_TIMEOUT_SECONDS}s"
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output[-CHECK_OUTPUT_LIMIT:]


def copy_repo(task, workspace):
    shutil.copytree(task["dir"] / "repo", workspace)


def check_solutions(tasks):
    """不调用模型，只检查任务本身是否有效：原始 repo 必须不通过，套上参考答案必须通过。"""
    broken = 0
    with tempfile.TemporaryDirectory() as temp:
        for task in tasks:
            base = Path(temp) / task["id"] / "base"
            copy_repo(task, base)
            base_passed, _ = run_check(task, base, "")

            solved = Path(temp) / task["id"] / "solved"
            copy_repo(task, solved)
            answer = ""
            for path in (task["dir"] / "solution").rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(task["dir"] / "solution")
                if relative.as_posix() == SOLUTION_ANSWER_FILE:
                    answer = path.read_text(encoding="utf-8")
                    continue
                target = solved / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            solved_passed, solved_output = run_check(task, solved, answer)

            ok = not base_passed and solved_passed
            broken += 0 if ok else 1
            print(f"{task['id']:24s} {'OK' if ok else 'BROKEN'}")
            if base_passed:
                print("  original repo passes the check: the check does not test the task")
            if not solved_passed:
                print("  reference solution fails the check:")
                print("  " + solved_output.replace("\n", "\n  "))
    return broken


def read_transcript(workspace, session_id):
    """从会话文件里取出这次试验的统计数据：会话文件是唯一的数据来源，和平时使用时完全一样。"""
    events = SessionStore(workspace / ".locagent" / "sessions").load(session_id)
    finished = next(event for event in reversed(events) if event["event"] == ev.RUN_FINISHED)
    tool_events = [event for event in events if event["event"] == ev.TOOL_FINISHED]
    usage = finished["usage"]
    input_total = usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
    return {
        "status": finished["status"],
        "stop_reason": finished["stop_reason"],
        "final_answer": finished["final_answer"],
        "attempts": finished["attempts"],
        "tool_steps": finished["tool_steps"],
        "tool_errors": sum(1 for event in tool_events if event["tool_status"] != "ok"),
        "tools_used": sorted({event["name"] for event in tool_events}),
        "notices": sum(1 for event in events if event["event"] == ev.NOTICE),
        "context_reduced": sum(1 for event in events if event["event"] == ev.CONTEXT_REDUCED),
        "input_tokens": input_total,
        "output_tokens": usage["output_tokens"],
        "cache_hit_rate": round(usage["cache_read_input_tokens"] / input_total, 3) if input_total else 0.0,
    }


def run_trial(task, trial, workspace, args):
    copy_repo(task, workspace)
    agent = LocAgent(
        model_client=_build_model_client(args),
        workspace=WorkspaceContext.build(workspace),
        # 评测时没人在旁边审批，全部自动同意；工作区是一次性副本
        approval_policy="auto",
        max_steps=task["max_steps"],
        max_new_tokens=args.max_new_tokens,
        context_budget=args.context_budget,
        allowed_tools=task.get("allowed_tools"),
    )
    if args.verbose:
        agent.events.subscribe(print_event)
    started = time.monotonic()
    error = ""
    try:
        agent.ask(task["prompt"])
    except RuntimeError as exc:
        # 模型请求失败：主循环已经写了 run_finished（model_error），记下原因继续下一次试验
        error = str(exc)
    row = {"task": task["id"], "kind": task["kind"], "category": task["category"], "trial": trial}
    row.update(read_transcript(workspace, agent.session_id))
    check_passed, check_output = run_check(task, workspace, row["final_answer"])
    finished = row["stop_reason"] == STOP_REASON_FINAL_ANSWER
    # 通过 = 判分通过，并且 agent 自己正常结束（没有因为步数用完、出错而中止）
    row["passed"] = check_passed and finished
    if row["passed"]:
        row["failure"] = ""
    elif row["stop_reason"] == STOP_REASON_MODEL_ERROR:
        row["failure"] = STOP_REASON_MODEL_ERROR
    elif not finished:
        row["failure"] = row["stop_reason"]
    else:
        row["failure"] = FAIL_CHECK
    # 声称完成但判分不通过：最需要关注的失败，用户会以为任务做好了
    row["false_completion"] = finished and not check_passed
    row["check_passed"] = check_passed
    row["check_output"] = check_output
    row["error"] = error
    row["duration_s"] = round(time.monotonic() - started, 1)
    row["workspace"] = str(workspace.relative_to(ROOT))
    row["session"] = str(agent.session_path.relative_to(ROOT))
    return row


def _mean(values):
    values = list(values)
    return round(sum(values) / len(values), 2) if values else 0.0


def summarize(tasks, rows):
    per_task = []
    for task in tasks:
        task_rows = [row for row in rows if row["task"] == task["id"]]
        if not task_rows:
            continue
        failures = {}
        for row in task_rows:
            if row["failure"]:
                failures[row["failure"]] = failures.get(row["failure"], 0) + 1
        passed = sum(1 for row in task_rows if row["passed"])
        per_task.append(
            {
                "task": task["id"],
                "kind": task["kind"],
                "category": task["category"],
                "passed": passed,
                "trials": len(task_rows),
                # pass@k：k 次里至少成功一次；pass^k：k 次全部成功（衡量稳定性）
                "pass_at_k": passed > 0,
                "pass_all_k": passed == len(task_rows),
                "avg_tool_steps": _mean(row["tool_steps"] for row in task_rows),
                "avg_attempts": _mean(row["attempts"] for row in task_rows),
                "avg_tool_errors": _mean(row["tool_errors"] for row in task_rows),
                "avg_input_tokens": _mean(row["input_tokens"] for row in task_rows),
                "avg_output_tokens": _mean(row["output_tokens"] for row in task_rows),
                "avg_cache_hit_rate": _mean(row["cache_hit_rate"] for row in task_rows),
                "avg_duration_s": _mean(row["duration_s"] for row in task_rows),
                "failures": failures,
            }
        )
    failures = {}
    for row in rows:
        if row["failure"]:
            failures[row["failure"]] = failures.get(row["failure"], 0) + 1
    overall = {
        "trials": len(rows),
        "passed": sum(1 for row in rows if row["passed"]),
        "pass_rate": _mean(1 if row["passed"] else 0 for row in rows),
        "pass_at_k": _mean(1 if item["pass_at_k"] else 0 for item in per_task),
        "pass_all_k": _mean(1 if item["pass_all_k"] else 0 for item in per_task),
        "false_completions": sum(1 for row in rows if row["false_completion"]),
        "failures": failures,
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "avg_cache_hit_rate": _mean(row["cache_hit_rate"] for row in rows),
    }
    by_kind = {}
    for kind in TASK_KINDS:
        kind_rows = [row for row in rows if row["kind"] == kind]
        if kind_rows:
            by_kind[kind] = {
                "trials": len(kind_rows),
                "passed": sum(1 for row in kind_rows if row["passed"]),
                "pass_rate": _mean(1 if row["passed"] else 0 for row in kind_rows),
            }
    return {"overall": overall, "by_kind": by_kind, "per_task": per_task}


def print_summary(summary, trials):
    overall = summary["overall"]
    print()
    print(f"pass rate      {overall['passed']}/{overall['trials']} = {overall['pass_rate']:.0%}")
    print(f"pass@{trials}         {overall['pass_at_k']:.0%} of tasks passed at least once")
    print(f"pass^{trials}         {overall['pass_all_k']:.0%} of tasks passed every time")
    print(f"false complete {overall['false_completions']} (answered as done, but the check failed)")
    print(f"failures       {overall['failures'] or '-'}")
    print(f"tokens         input {overall['input_tokens']}, output {overall['output_tokens']}, cache hit {overall['avg_cache_hit_rate']:.0%}")
    for kind, item in summary["by_kind"].items():
        print(f"{kind:14s} {item['passed']}/{item['trials']} = {item['pass_rate']:.0%}")
    print()
    print(f"{'task':24s} {'kind':11s} {'pass':6s} {'steps':>6s} {'errors':>7s} {'in_tok':>8s} {'out_tok':>8s} {'secs':>6s}  failures")
    for item in summary["per_task"]:
        print(
            f"{item['task']:24s} {item['kind']:11s} {str(item['passed']) + '/' + str(item['trials']):6s} "
            f"{item['avg_tool_steps']:6.1f} {item['avg_tool_errors']:7.1f} {item['avg_input_tokens']:8.0f} "
            f"{item['avg_output_tokens']:8.0f} {item['avg_duration_s']:6.1f}  {item['failures'] or ''}"
        )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="locagent-eval",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Run the LocAgent evaluation tasks against the real model.",
    )
    parser.add_argument("--tasks", nargs="*", default=None, help="Task ids to run. Defaults to all tasks.")
    parser.add_argument("--trials", type=int, default=3, help="Trials per task.")
    parser.add_argument("--check-solutions", action="store_true", help="Only validate the tasks: the original repo must fail and the reference solution must pass. No model calls.")
    parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR), help="Directory that holds the tasks.")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="Output root directory.")
    parser.add_argument("--verbose", action="store_true", help="Print every event, like the interactive CLI.")
    parser.add_argument("--model", default=None, help="Qwen model name. Defaults to QWEN_MODEL, then qwen3.6-plus.")
    parser.add_argument("--base-url", default=None, help="Qwen API base URL.")
    parser.add_argument("--timeout", type=int, default=300, help="Model request timeout in seconds.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    parser.add_argument("--max-new-tokens", type=int, default=8192, help="Maximum model output tokens per step.")
    parser.add_argument("--context-budget", type=int, default=DEFAULT_CONTEXT_BUDGET, help="Input token budget.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        tasks = load_tasks(args.tasks_dir, args.tasks)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # .env 里的密钥既用于请求模型，也用于 secret_guard 任务的判分
    load_project_env(ROOT)
    if args.check_solutions:
        broken = check_solutions(tasks)
        print(f"\n{len(tasks) - broken}/{len(tasks)} tasks valid")
        return 1 if broken else 0

    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    print(f"output: {out_dir}")
    results_path = out_dir / "results.jsonl"
    rows = []
    try:
        for trial in range(1, args.trials + 1):
            print(f"trial {trial}/{args.trials}")
            for task in tasks:
                row = run_trial(task, trial, out_dir / task["id"] / f"trial_{trial}", args)
                rows.append(row)
                # 逐行追加：中途按 Ctrl+C 停止，已完成的试验结果也保留
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                mark = "PASS" if row["passed"] else f"FAIL ({row['failure']})"
                print(
                    f"  {task['id']:24s} {mark:28s} steps={row['tool_steps']} errors={row['tool_errors']} "
                    f"tokens={row['input_tokens']}/{row['output_tokens']} {row['duration_s']}s"
                )
                if not row["passed"]:
                    # 失败原因：模型出错时是错误信息，否则是判分输出的最后一行（断言信息）
                    reason = row["error"] or (row["check_output"].splitlines() or ["-"])[-1]
                    print(f"    {middle(reason, 200)}")
    except KeyboardInterrupt:
        print("\ninterrupted, summarizing finished trials")

    if not rows:
        return 1
    summary = summarize(tasks, rows)
    summary["config"] = {
        "model": qwen_settings(model=args.model, base_url=args.base_url)["model"],
        "trials": args.trials,
        "max_new_tokens": args.max_new_tokens,
        "context_budget": args.context_budget,
        "temperature": args.temperature,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_summary(summary, args.trials)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

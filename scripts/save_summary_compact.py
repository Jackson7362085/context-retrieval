#!/usr/bin/env python3
import json
import os
import re
import sys
import textwrap
import subprocess

from anthropic import Anthropic
from datetime import datetime

# === 用于美化输出的 ANSI 颜色代码 ===
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'  # 推荐用这个颜色做流式输出
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'   # 重置颜色
    BOLD = '\033[1m'

# /save-summary 不再需要参数
# CMD_RE = re.compile(r"^\s*/(?:context-retrieval:)?save-summary\s*$")


FILE_BLOCK_RE = re.compile(
    r"===FILE:index\.json===\s*(?P<index>.*?)\s*===END_FILE===\s*"
    r"===FILE:resolutions\.ndjson===\s*(?P<ndjson>.*?)\s*===END_FILE===",
    re.DOTALL,
)

def eprint(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")


def tail_lines(path: str, max_lines: int = 4000) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-max_lines:]
    except FileNotFoundError:
        return []

def parse_jsonl_lines(lines: list[str]) -> list[dict]:
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out

def collect_text_from_any(node, out: list[str]) -> None:
    """
    递归从任意结构里收集文本：
    - str
    - list
    - dict: 支持 {"type":"text","text":...} / {"type":"tool_result","content":[...]} / {"message":...} 等
    """
    if node is None:
        return
    if isinstance(node, str):
        s = node.strip()
        if s:
            out.append(s)
        return
    if isinstance(node, list):
        for item in node:
            collect_text_from_any(item, out)
        return
    if isinstance(node, dict):
        # 常见文本块
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            s = node["text"].strip()
            if s:
                out.append(s)
        # 递归常见容器字段
        if "content" in node:
            collect_text_from_any(node["content"], out)
        if "message" in node:
            collect_text_from_any(node["message"], out)
        if "input" in node:
            # tool_use 的 input 里可能有 prompt/description
            collect_text_from_any(node["input"], out)

        return

def extract_text_from_message_obj(msg_obj) -> str:
    """
    兼容：
    - message.content 是 str
    - message.content 是 list，其中包含 text / tool_result / 其他嵌套结构
    """
    if not isinstance(msg_obj, dict):
        return ""
    buf: list[str] = []
    collect_text_from_any(msg_obj.get("content"), buf)
    return "\n".join([x for x in buf if x]).strip()

def build_session_material(events: list[dict], max_chars: int = 60000) -> str:
    """
    目标：把“复杂 transcript”的主要信息抓全：
    - summary 事件
    - 普通对话（user/assistant）
    - tool_use（工具调用：name/id/input 里的 description/prompt）
    - tool_result（工具输出的大段文本）
    - 顶层 toolUseResult（统计 + content）
    """
    summaries: list[str] = []
    dialogue: list[str] = []
    tool_uses: list[str] = []
    tool_results: list[str] = []
    tool_stats: list[str] = []

    for e in events:
        if not isinstance(e, dict):
            continue

        # 1) summary
        if e.get("type") == "summary" and isinstance(e.get("summary"), str):
            summaries.append(e["summary"].strip())
            continue

        # 2) tool_use（通常在 assistant.message.content[] 里）
        if e.get("type") == "assistant":
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        name = item.get("name")
                        tid = item.get("id")
                        inp = item.get("input")
                        desc = ""
                        if isinstance(inp, dict):
                            if isinstance(inp.get("description"), str):
                                desc = inp["description"]
                            elif isinstance(inp.get("prompt"), str):
                                desc = inp["prompt"]
                        desc = (desc or "").strip().replace("\r", "")
                        tool_uses.append(f"- tool_use: {name} ({tid}) {desc[:600]}")

        # 3) tool_result（复杂块通常在 type:"user" 的 message.content[] 里）
        if e.get("type") == "user":
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        buf: list[str] = []
                        collect_text_from_any(item.get("content"), buf)
                        block = "\n".join([x for x in buf if x]).strip()
                        if block:
                            tool_results.append(block)

        # 4) 普通对话（user/assistant）
        t = e.get("type")
        if t in ("user", "assistant"):
            msg_obj = e.get("message")
            if isinstance(msg_obj, dict):
                role = msg_obj.get("role") or t
                if isinstance(role, str):
                    role = role.strip()
                else:
                    role = t
                text = extract_text_from_message_obj(msg_obj)
                if text:
                    dialogue.append(f"{role}: {text}")

        # 5) 顶层 toolUseResult（统计 + content）
        tur = e.get("toolUseResult")
        if isinstance(tur, dict):
            status = tur.get("status")
            agent_id = tur.get("agentId")
            total_tokens = tur.get("totalTokens")
            duration = tur.get("totalDurationMs")
            tool_stats.append(
                f"- toolUseResult: status={status}, agentId={agent_id}, totalTokens={total_tokens}, durationMs={duration}"
            )
            buf: list[str] = []
            collect_text_from_any(tur.get("content"), buf)
            block = "\n".join([x for x in buf if x]).strip()
            if block:
                tool_results.append(block)

    material: list[str] = []

    if summaries:
        material.append("【summary 事件】")
        material += [f"- {s}" for s in summaries[-80:]]
        material.append("")

    if tool_uses:
        material.append("【tool_use 事件（截取）】")
        material += tool_uses[-120:]
        material.append("")

    if tool_stats:
        material.append("【toolUseResult 统计】")
        material += tool_stats[-120:]
        material.append("")

    if tool_results:
        material.append("【tool_result / toolUseResult 输出（重点，截取）】")
        for block in tool_results[-120:]:
            material.append(block[:20000])
            material.append("\n---\n")

    if dialogue:
        material.append("【最近对话（截取）】")
        material += dialogue[-120:]

    blob = "\n".join(material).strip()
    return blob[-max_chars:]


def load_project_settings() -> dict:
    """
    只从用户级 ~/.claude/settings.json 加载配置
    """
    settings_path = os.path.expanduser("~/.claude/settings.json")

    if not os.path.isfile(settings_path):
        sys.stderr.write(
            f"⚠️ 用户级配置不存在: {settings_path}\n"
        )
        return {}

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
            sys.stderr.write(
                f"✅ Loaded user settings from: {settings_path}\n"
            )
            return settings
    except Exception as e:
        sys.stderr.write(
            f"⚠️ 读取 {settings_path} 失败: {e}\n"
        )
        return {}


def run_claude_p(prompt: str, input_text: str) -> str:
    """
    使用 Anthropic SDK 调用自定义 API 接口
    替换了原本不稳定的命令行调用方式
    """
    
    # # === 配置区域 (根据你提供的 Settings 填入) ===
    # # 对应 settings: ANTHROPIC_AUTH_TOKEN
    # API_KEY = "alkjhiu8970876iulkhio^&khh" 
    
    # # 对应 settings: ANTHROPIC_BASE_URL
    # BASE_URL = "http://azure.711511.xyz:8317"
    
    # # 对应 settings: ANTHROPIC_REASONING_MODEL
    # # 注意：你说要用这个 thinking 模型
    # MODEL_NAME = "gemini-claude-opus-4-5-thinking"
    
    # # ===========================================

    """
    读取 settings.json 配置并使用 Anthropic SDK 调用 API
    """
    
    # === 1. 从 settings.json 加载配置 ===
    settings = load_project_settings()
    
    # 【关键修改点】：配置在 "env" 对象下面，而不是根节点
    # 使用 .get("env", {}) 确保即使 env 不存在也不会报错，而是返回空字典
    env_config = settings.get("env", {})
    
    # 从 env_config 中提取配置
    api_key = env_config.get("ANTHROPIC_AUTH_TOKEN")
    base_url = env_config.get("ANTHROPIC_BASE_URL")
    
    # 模型名称也优先从 env 中读取
    model_name = env_config.get("ANTHROPIC_REASONING_MODEL", "gemini-claude-opus-4-5-thinking")
    
    # === 2. 校验必要配置 ===
    if not api_key:
        raise ValueError("settings.json 的 'env' 中缺少 'ANTHROPIC_AUTH_TOKEN'。")
    
    if not base_url:
        raise ValueError("settings.json 的 'env' 中缺少 'ANTHROPIC_BASE_URL'。")
    # 1. 初始化客户端
    try:
        client = Anthropic(
            api_key=api_key,
            base_url=base_url,
        )
    except Exception as e:
        raise RuntimeError(f"Anthropic SDK 初始化失败: {e}")

    # 2. 构造消息
    # 为了兼容性最强，我们将 Prompt（指令）和 input_text（素材）合并发给 User
    full_content = f"{prompt}\n\n{input_text}"

    # 3. 发送请求
    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=8192,  # 设置足够大的输出 Token，防止 JSON 被截断
            temperature=0.1,  # 归档任务建议低温度，保证格式稳定
            messages=[
                {
                    "role": "user", 
                    "content": full_content
                }
            ]
        )
        
        # 4. 获取并返回文本
        # Anthropic 的返回结构通常是 content[0].text
        if not response.content:
            raise RuntimeError("API 返回了空 content")
            
        return response.content[0].text

    except Exception as e:
        # 这里会捕获如 401(认证失败), 400(请求过长), 500(服务器错) 等所有 API 错误
        # 并将错误信息抛出，这样你的 debug 文件里就能看到具体的 API 报错原因了
        raise RuntimeError(f"API 请求发生错误: {str(e)}")



def normalize_index(obj: dict) -> dict:
    """
    防止模型偶发漏字段导致后续合并崩溃：保证 schema 字段存在。
    """
    if not isinstance(obj, dict):
        obj = {}
    obj.setdefault("context_version", "v2")
    obj.setdefault("project", "unknown")
    obj.setdefault("current_state", "")
    obj.setdefault("goals", [])
    obj.setdefault("constraints", [])
    obj.setdefault("environment", {"os": None, "runtime": None, "tools": [], "paths": []})
    obj.setdefault("verified_facts", [])
    obj.setdefault("next_actions", [])
    obj.setdefault("detail_index", {"resolutions": []})
    if not isinstance(obj.get("detail_index"), dict):
        obj["detail_index"] = {"resolutions": []}
    obj["detail_index"].setdefault("resolutions", [])
    if not isinstance(obj["detail_index"]["resolutions"], list):
        obj["detail_index"]["resolutions"] = []
    return obj

def unique_list_merge(a, b) -> list:
    """
    合并两个 list，按 json 序列化后的 key 去重，保持顺序（先 a 后 b）
    """
    out = []
    seen = set()
    for arr in (a, b):
        if not isinstance(arr, list):
            continue
        for item in arr:
            try:
                k = json.dumps(item, ensure_ascii=False, sort_keys=True)
            except Exception:
                k = str(item)
            if k in seen:
                continue
            seen.add(k)
            out.append(item)
    return out

def merge_resolution_index_items(existing_items: list, incoming_items: list) -> list:
    """
    合并 detail_index.resolutions：按 id 去重（id 作为磁盘主键）。
    """
    out = []
    seen = set()
    for it in (existing_items or []):
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            rid = it["id"]
            if rid not in seen:
                out.append(it)
                seen.add(rid)
    for it in (incoming_items or []):
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            rid = it["id"]
            if rid not in seen:
                out.append(it)
                seen.add(rid)
    return out

def allocate_next_res_id(res_dir: str, reserved: set[str]) -> str:
    os.makedirs(res_dir, exist_ok=True)
    existing = set()
    for name in os.listdir(res_dir):
        if name.startswith("res-") and name.endswith(".json"):
            existing.add(name[:-5])
    n = 1
    while True:
        rid = f"res-{n:03d}"
        if rid not in existing and rid not in reserved:
            reserved.add(rid)
            return rid
        n += 1

def parse_model_output_to_context(out_text: str, claude_dir: str) -> dict:
    """
    解析模型输出的两个文件块，并落盘到：
    - {claude_dir}/context/index.json
    - {claude_dir}/context/resolutions/res-00x.json
    返回 index.json 的 dict
    """
    m = FILE_BLOCK_RE.search(out_text)
    if not m:
        raise ValueError("模型输出不符合预期分隔符格式（未找到 index.json / resolutions.ndjson 文件块）")

    incoming_index = normalize_index(json.loads(m.group("index").strip()))

    ndjson_text = m.group("ndjson").strip()


    context_dir = os.path.join(claude_dir, "context")
    res_dir = os.path.join(context_dir, "resolutions")
    os.makedirs(res_dir, exist_ok=True)

    index_path = os.path.join(context_dir, "index.json")


    # 1) 读已有 index（用于合并）
    existing_index = None
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                existing_index = normalize_index(json.load(f))
        except Exception:
            existing_index = None

    # 2) 逐行写入 resolutions（追加式，避免覆盖）
    id_remap: dict[str, str] = {}    # 模型id -> 实际落盘id（冲突时）
    written_ids: list[str] = []
    reserved_ids: set[str] = set()


    lines = [ln.strip() for ln in ndjson_text.splitlines() if ln.strip()]
    for ln in lines:
        obj = json.loads(ln)
        rid = obj.get("id")
        if not isinstance(rid, str) or not rid.startswith("res-"):
            raise ValueError(f"resolution 行缺少合法 id：{rid!r}")
       
        out_file = os.path.join(res_dir, f"{rid}.json")
        if os.path.exists(out_file) or rid in reserved_ids:
            new_rid = allocate_next_res_id(res_dir, reserved_ids)

            id_remap[rid] = new_rid
            rid = new_rid
            obj["id"] = rid
            out_file = os.path.join(res_dir, f"{rid}.json")
        else:
            reserved_ids.add(rid)


        with open(out_file, "w", encoding="utf-8") as f:

            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")


        written_ids.append(rid)


    # 3) 把 incoming_index.detail_index.resolutions 里的 id 按 remap 修正
    incoming_items = incoming_index.get("detail_index", {}).get("resolutions", [])
    if isinstance(incoming_items, list):
        for it in incoming_items:
            if isinstance(it, dict) and isinstance(it.get("id"), str):
                old = it["id"]
                if old in id_remap:
                    it["id"] = id_remap[old]

    # 如果模型没输出 detail_index（或空），但我们确实写了 resolutions，则补最小目录项
    if (not isinstance(incoming_items, list) or not incoming_items) and written_ids:
        incoming_index["detail_index"]["resolutions"] = []
        for rid in written_ids:
            incoming_index["detail_index"]["resolutions"].append({

                "id": rid,
                "problem_signature": "",
                "summary": "",
                "tags": [],
                "artifacts_touched": [],
            })


    # 4) 合并 index（目录永不丢）
    if existing_index:
        merged = existing_index

        # 最新覆盖（代表当前状态）
        merged["project"] = incoming_index.get("project", merged.get("project"))
        merged["current_state"] = incoming_index.get("current_state", merged.get("current_state"))
        merged["next_actions"] = incoming_index.get("next_actions", merged.get("next_actions"))

        # 并集合并（不丢历史）
        merged["goals"] = unique_list_merge(merged.get("goals", []), incoming_index.get("goals", []))
        merged["constraints"] = unique_list_merge(merged.get("constraints", []), incoming_index.get("constraints", []))
        merged["verified_facts"] = unique_list_merge(merged.get("verified_facts", []), incoming_index.get("verified_facts", []))

        # environment 合并（os/runtime 最新覆盖；tools/paths 并集）
        env_old = merged.get("environment") if isinstance(merged.get("environment"), dict) else {}
        env_new = incoming_index.get("environment") if isinstance(incoming_index.get("environment"), dict) else {}
        env = {
            "os": env_new.get("os", env_old.get("os")),
            "runtime": env_new.get("runtime", env_old.get("runtime")),
            "tools": unique_list_merge(env_old.get("tools", []), env_new.get("tools", [])),
            "paths": unique_list_merge(env_old.get("paths", []), env_new.get("paths", [])),
        }
        merged["environment"] = env

        merged["detail_index"]["resolutions"] = merge_resolution_index_items(
            merged.get("detail_index", {}).get("resolutions", []),
            incoming_index.get("detail_index", {}).get("resolutions", []),
        )
        final_index = merged
    else:
        final_index = incoming_index

    # 5) 写回 index.json（这是“聚合目录”的最新版本，不会删除旧条目）
    os.makedirs(context_dir, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(final_index, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return final_index


def main() -> None:
    raw_stdin = sys.stdin.read()

    try:
        data = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except Exception as ex:
        eprint(f"❌ hook stdin 不是合法 JSON：{ex}")
        sys.exit(2)

    # user_prompt = (data.get("prompt") or "").strip()
    # m = CMD_RE.match(user_prompt)
    # if not m:
    #     sys.exit(0)


    # 更稳：优先项目根，再 fallback 到 cwd
    base_dir = (os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())

    claude_dir = os.path.join(base_dir, ".claude")
    context_dir = os.path.join(claude_dir, "context")
    debug_dir = os.path.join(context_dir, "debug")
    os.makedirs(debug_dir, exist_ok=True)


    transcript_path = os.path.expanduser(data.get("transcript_path", "") or "")
    lines = tail_lines(transcript_path, max_lines=8000)
    events = parse_jsonl_lines(lines)
    material = build_session_material(events)

    # 你的“对话归档器”提示词（保持你现在的版本）
    summary_prompt = textwrap.dedent("""\
你是一个“对话归档器（Conversation Archivist）”，负责把用户与编程工具/Agent 的长对话，提炼为可复用的工程上下文。
你的输出必须是两个文件：index.json 和 resolutions.ndjson，且必须严格遵守下面的格式要求，便于程序后续拆分成目录与单文件。

【输入】
用户将提供一段长对话文本，可能包含：
- 目标描述、约束、环境信息
- 多次失败与修复尝试（日志）
- 最终成功的方案（非常重要）
- 对多个代码/配置文件的修改点（不需要贴全文）
- 验证步骤与结果

【你要做的事】
1) 识别“项目/任务”的当前目标与状态（current_state）
2) 提取“已验证事实”（verified_facts）
3) 从反复尝试中提炼“最终改对的解决方案剧本（resolution）”
   - 每个 resolution 必须能被复现：包含 final_fix 步骤、verification 检查点、why_it_works
   - 必须提取 1-3 条 anti_patterns（走过但没用/有坑）
4) 为每条 resolution 生成稳定可检索的 problem_signature（尽量来自日志中的稳定报错片段/关键词）
5) 不要输出任何文件全文内容；只记录关键改动点与触达文件（artifacts_touched），让编程工具自行读取。

【输出要求 - 总体】
- 只输出两个“文件块”，按如下分隔符输出，方便程序拆分：
  ===FILE:index.json===
  <这里是完整 JSON>
  ===END_FILE===
  ===FILE:resolutions.ndjson===
  <这里是 NDJSON，每行一个 JSON对象>
  ===END_FILE===
- 除这两个文件块之外，不允许输出任何额外文字、解释、markdown、代码块。

【index.json Schema】
index.json 必须是严格 JSON 对象，字段如下（可为空但必须存在）：
{
  "context_version": "v2",
  "project": "<string: 项目名/任务名，若不明确就用 'unknown'>",
  "current_state": "<string: 例如 setup_in_progress / cluster_ok_tested / build_failed 等>",
  "goals": ["<string>", "..."],
  "constraints": ["<string>", "..."],
  "environment": {
    "os": "<string|null>",
    "runtime": "<string|null>",
    "tools": ["<string>", "..."],
    "paths": ["<string>", "..."]
  },
  "verified_facts": ["<string>", "..."],
  "next_actions": ["<string>", "..."],
  "detail_index": {
    "resolutions": [
      {
        "id": "res-001",
        "problem_signature": "<string: 可用于匹配日志/报错的稳定关键词>",
        "summary": "<string: 一句话最终正确方案>",
        "tags": ["<string>", "..."],
        "artifacts_touched": ["<string>", "..."]
      }
    ]
  }
}

约束：
- detail_index 只收录 resolutions（本任务只产出 index+resolutions）
- artifacts_touched 只写文件路径/组件名，不要贴文件内容
- verified_facts 必须是“已经确认”的事实，不要猜测
- goals/constraints/environment 若输入没有就留空数组或 null，但字段必须存在

【resolutions.ndjson Schema】
resolutions.ndjson 每行一个 JSON 对象，字段如下（每条都必须完整）：
{
  "id": "res-001",
  "type": "resolution",
  "problem_signature": "<string>",
  "problem": "<string: 现象/报错>",
  "root_cause": "<string: 最终原因>",
  "final_fix": ["<string step>", "..."],
  "why_it_works": "<string>",
  "verification": ["<string check>", "..."],
  "anti_patterns": ["<string>", "..."],
  "artifacts_touched": ["<string>", "..."],
  "evidence": {
    "signals": ["<string: 来自对话的关键证据，如 'cluster_state:ok' 或 '[OK] All 16384 slots covered'>", "..."],
    "when": "<string|null: 若对话里有日期时间就填，否则 null>"
  }
}

约束：
- final_fix 与 verification 必须可执行/可验证（步骤清晰）
- anti_patterns 最少 1 条，最多 3 条
- evidence.signals 用来存“成功判据/关键 log 片段”，不要贴长日志（每条≤120字符）
- id 从 res-001 递增，不跳号

【提取策略】
- 如果同一个问题出现多次尝试，以“最后成功的那条路径”为准写 final_fix
- 如果对话没有明确成功证据，就不要编造 verified_facts；相应 resolution 的 evidence.signals 也要谨慎
- problem_signature 优先取稳定报错关键词，如：
  - "Pool overlaps with other one"
  - "the input device is not a TTY"
  - "CROSSSLOT Keys in request"
  - "container name already in use"
  - "cluster_state:ok"
- tags 用小写短词：docker/compose/redis/network/windows/tty/cleanup/cluster/slots 等

现在开始处理用户提供的对话文本，并按上述格式直接输出两个文件块。
    """).strip()

    engine = "claude-cli"
    model_out = ""
    index_err = None
    try:
        model_out = run_claude_p(summary_prompt, "【会话素材】\n" + material)
        # 解析模型输出并写入 .claude/context/...
        claude_dir = os.path.join(base_dir, ".claude")
        parse_model_output_to_context(model_out, claude_dir)
    except Exception as ex:
        engine = "error"
        index_err = str(ex)

    # 固定写 debug 文件，便于排错（不再需要用户指定输出文件）
    now = datetime.now().isoformat(timespec="seconds").replace(":", "-")
    debug_path = os.path.join(debug_dir, f"save-summary-{now}.md")
    with open(debug_path, "w", encoding="utf-8") as f:

        f.write(f"<!-- saved: {now} | engine: {engine} -->\n\n")

        f.write("# Hook 原始输入（stdin 原文）\n\n```json\n")
        f.write(raw_stdin.rstrip() + "\n")
        f.write("```\n\n")

        f.write("# transcript_path\n\n")
        f.write(f"`{transcript_path}`\n\n")

        f.write("# 会话素材（给模型看的输入）\n\n```text\n")
        f.write(material.rstrip() + "\n")
        f.write("```\n\n")

        f.write("# 模型原始输出（用于归档解析）\n\n```text\n")
        f.write((model_out or "").rstrip() + "\n")
        f.write("```\n\n")

        if index_err:
            f.write("# 解析/落盘错误\n\n")
            f.write(index_err + "\n")
    if engine == "error":
        eprint(f"⚠️ /save-summary 失败：{index_err}\n调试文件：{debug_path}")
    else:
        
        # 计算一下文件路径
        context_path = os.path.join(claude_dir, 'context')
        
        eprint(f"\n{Colors.GREEN}✔ [SYSTEM SYNC] Archive Successful.{Colors.ENDC}")
        eprint(f"{Colors.BOLD}📂 Context Updated: {Colors.ENDC}{context_path}")
        eprint(f"{Colors.BOLD}📝 Debug Log:      {Colors.ENDC}{os.path.basename(debug_path)}")

    sys.exit(2)

if __name__ == "__main__":
    sys.stderr.write(
        f"DEBUG env: "
        f"CLAUDE_PLUGIN_ROOT={os.environ.get('CLAUDE_PLUGIN_ROOT')}, "
        f"CLAUDE_PROJECT_DIR={os.environ.get('CLAUDE_PROJECT_DIR')}\n"
        )
    main()

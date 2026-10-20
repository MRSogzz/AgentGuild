"""交付摘要構建。"""


def build_final_summary(user_msg, responses, all_messages, refine_round,
                        artifacts, verdict, stop_reason=None):
    lines = [f"任務：{user_msg[:100]}"]
    if verdict == "DONE":
        lines.append("狀態：完成（部分 Agent 未執行）")
    else:
        lines.append(f"最終判定：{'通過 ✓' if verdict == 'PASS' else '仍有待改進'}")
        lines.append(f"改進輪數：{refine_round}")
        if stop_reason:
            lines.append(f"停止原因：{stop_reason}")
    dev_versions = [m for m in all_messages if m["agent_id"] == "dev"]
    lines.append(f"工程師輸出：{len(dev_versions)} 個版本")
    if artifacts:
        file_list = []
        for logical_name, versions in artifacts.items():
            if versions:
                file_list.append(logical_name)
        if file_list:
            lines.append(f"程式碼工件：{', '.join(file_list)}")
    return "\n".join(lines)
"""
导出模块
支持HTML、Markdown、JSON格式导出
"""
import json
import base64
import webbrowser
from pathlib import Path
from typing import Optional
from datetime import datetime

from session_manager import Session
from utils import (
    get_exports_dir,
    copy_files_to_export,
    strip_ansi,
    is_image_file
)


class Exporter:
    """会话导出器"""

    def __init__(self):
        self.exports_dir = get_exports_dir()

    def export(self, session: Session, format: str = 'html', open_after: bool = True) -> Path:
        """
        导出会话

        Args:
            session: 会话对象
            format: 导出格式 ('html', 'markdown', 'json')
            open_after: 导出后是否打开

        Returns:
            导出文件路径
        """
        export_dir = self.exports_dir / f"{session.session_id}_{format}"
        export_dir.mkdir(exist_ok=True)

        # 复制引用的文件
        all_files = session.get_all_files()
        file_mapping = copy_files_to_export(all_files, export_dir)

        if format == 'html':
            output_path = self._export_html(session, export_dir, file_mapping)
        elif format == 'markdown':
            output_path = self._export_markdown(session, export_dir, file_mapping)
        elif format == 'json':
            output_path = self._export_json(session, export_dir, file_mapping)
        else:
            raise ValueError(f"Unsupported format: {format}")

        if open_after and format == 'html':
            webbrowser.open(f'file://{output_path}')

        return output_path

    def _export_html(self, session: Session, export_dir: Path, file_mapping: dict) -> Path:
        """导出为HTML"""
        html_content = self._generate_html(session, file_mapping)
        output_path = export_dir / "session.html"

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        return output_path

    def _generate_html(self, session: Session, file_mapping: dict) -> str:
        """生成HTML内容"""
        entries_html = []

        # 预处理：过滤掉纯回显的OUTPUT（紧跟在INPUT之前且内容相似的OUTPUT）
        filtered_entries = self._filter_echo_entries(session.entries)

        for entry in filtered_entries:
            entry_class = 'input' if entry.type == 'input' else 'output'

            # 清理内容（会自动去除ANSI代码和不必要的行）
            content = self._clean_whitespace(entry.content)

            # 跳过空内容
            if not content.strip():
                continue

            # HTML转义
            content = self._escape_html(content)

            # 处理 diff 格式的颜色（添加/删除行）
            content = self._colorize_diff(content)

            # 处理文件引用
            files_html = ""
            if entry.files:
                files_html = '<div class="files">'
                for f in entry.files:
                    rel_path = file_mapping.get(f, f)
                    if is_image_file(f):
                        files_html += f'<div class="file-preview"><img src="{rel_path}" alt="{Path(f).name}"><span>{Path(f).name}</span></div>'
                    else:
                        files_html += f'<div class="file-link"><a href="{rel_path}">{Path(f).name}</a></div>'
                files_html += '</div>'

            entries_html.append(f'''
            <div class="entry {entry_class}">
                <div class="entry-header">
                    <span class="type">{entry.type.upper()}</span>
                    <span class="time">{entry.timestamp}</span>
                </div>
                <pre class="content">{content}</pre>
                {files_html}
            </div>
            ''')

        return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>会话记录 - {session.session_id}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: 'SF Mono', 'Monaco', 'Inconsolata', 'Roboto Mono', monospace;
            background: #1a1a2e;
            color: #eee;
            padding: 20px;
            line-height: 1.6;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 30px;
            border-radius: 12px;
            margin-bottom: 30px;
            box-shadow: 0 10px 40px rgba(102, 126, 234, 0.3);
        }}
        .header h1 {{
            font-size: 1.8em;
            margin-bottom: 15px;
        }}
        .header .meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 20px;
            font-size: 0.9em;
            opacity: 0.9;
        }}
        .header .meta span {{
            background: rgba(255,255,255,0.2);
            padding: 5px 12px;
            border-radius: 20px;
        }}
        .entry {{
            background: #16213e;
            border-radius: 8px;
            margin-bottom: 15px;
            overflow: hidden;
            border-left: 4px solid #667eea;
            transition: transform 0.2s;
        }}
        .entry:hover {{
            transform: translateX(5px);
        }}
        .entry.input {{
            border-left-color: #4ade80;
        }}
        .entry.output {{
            border-left-color: #60a5fa;
        }}
        .entry-header {{
            display: flex;
            justify-content: space-between;
            padding: 10px 15px;
            background: rgba(255,255,255,0.05);
            font-size: 0.85em;
        }}
        .entry-header .type {{
            font-weight: bold;
            text-transform: uppercase;
        }}
        .entry.input .type {{ color: #4ade80; }}
        .entry.output .type {{ color: #60a5fa; }}
        .entry-header .time {{
            color: #888;
        }}
        .content {{
            padding: 15px;
            white-space: pre;
            font-size: 0.9em;
            overflow-x: auto;
            color: #e0e0e0;
            line-height: 1.5;
        }}
        .files {{
            padding: 15px;
            background: rgba(255,255,255,0.03);
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
        }}
        .file-preview {{
            text-align: center;
        }}
        .file-preview img {{
            max-width: 300px;
            max-height: 200px;
            border-radius: 8px;
            border: 2px solid #333;
        }}
        .file-preview span {{
            display: block;
            margin-top: 5px;
            font-size: 0.8em;
            color: #888;
        }}
        .file-link a {{
            color: #60a5fa;
            text-decoration: none;
            padding: 8px 15px;
            background: rgba(96, 165, 250, 0.1);
            border-radius: 5px;
            display: inline-block;
        }}
        .file-link a:hover {{
            background: rgba(96, 165, 250, 0.2);
        }}
        /* ANSI颜色 - 在深色背景上可见 */
        .ansi-black {{ color: #9ca3af; }}
        .ansi-red {{ color: #ff6b6b; }}
        .ansi-green {{ color: #4ade80; }}
        .ansi-yellow {{ color: #fbbf24; }}
        .ansi-blue {{ color: #60a5fa; }}
        .ansi-magenta {{ color: #c084fc; }}
        .ansi-cyan {{ color: #22d3ee; }}
        .ansi-white {{ color: #fff; }}
        .ansi-bold {{ font-weight: bold; color: #fff; }}
        .ansi-dim {{ color: #b0b0b0; }}
        /* 确保所有span内的文本可见 */
        .content span {{ color: inherit; }}
        .content span:empty {{ display: none; }}
        /* Diff 颜色 */
        .diff-add {{ color: #4ade80; }}  /* 绿色 - 添加的行 */
        .diff-del {{ color: #f87171; }}  /* 红色 - 删除的行 */
    </style>
</head>
<body>
    <div class="header">
        <h1>智能终端 - 会话记录</h1>
        <div class="meta">
            <span>会话ID: {session.session_id}</span>
            <span>命令: {session.command}</span>
            <span>开始: {session.start_time}</span>
            <span>结束: {session.end_time or 'N/A'}</span>
            <span>工作目录: {session.working_directory}</span>
        </div>
    </div>
    <div class="entries">
        {''.join(entries_html)}
    </div>
</body>
</html>'''

    def _export_markdown(self, session: Session, export_dir: Path, file_mapping: dict) -> Path:
        """导出为Markdown"""
        lines = [
            f"# 会话记录 - {session.session_id}",
            "",
            "## 会话信息",
            f"- **会话ID**: {session.session_id}",
            f"- **命令**: {session.command}",
            f"- **开始时间**: {session.start_time}",
            f"- **结束时间**: {session.end_time or 'N/A'}",
            f"- **工作目录**: {session.working_directory}",
            "",
            "---",
            "",
            "## 交互记录",
            ""
        ]

        # 过滤回显条目
        filtered_entries = self._filter_echo_entries(session.entries)

        for i, entry in enumerate(filtered_entries, 1):
            type_label = "输入" if entry.type == 'input' else "输出"
            # 清理内容（会自动去除ANSI代码和不必要的行）
            content = self._clean_whitespace(entry.content)

            # 跳过空内容
            if not content.strip():
                continue

            lines.append(f"### [{i}] {type_label} - {entry.timestamp}")
            lines.append("")
            lines.append("```")
            lines.append(content)
            lines.append("```")
            lines.append("")

            if entry.files:
                lines.append("**相关文件:**")
                for f in entry.files:
                    rel_path = file_mapping.get(f, f)
                    if is_image_file(f):
                        lines.append(f"![{Path(f).name}]({rel_path})")
                    else:
                        lines.append(f"- [{Path(f).name}]({rel_path})")
                lines.append("")

        output_path = export_dir / "session.md"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        return output_path

    def _export_json(self, session: Session, export_dir: Path, file_mapping: dict) -> Path:
        """导出为JSON - 训练数据格式（支持多模态）"""
        # 过滤和清理条目
        filtered_entries = self._filter_echo_entries(session.entries)

        # 转换为训练格式的消息列表
        messages = []
        for entry in filtered_entries:
            content_text = self._clean_whitespace(entry.content)

            # 检查是否有图片文件
            image_files = [f for f in entry.files if is_image_file(f)]

            role = "user" if entry.type == "input" else "assistant"

            if image_files:
                # 多模态内容：包含文本和图片
                content_parts = []

                # 添加图片（base64编码）
                for img_path in image_files:
                    img_base64 = self._image_to_base64(img_path)
                    if img_base64:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": img_base64
                            }
                        })

                # 添加文本（移除图片路径）
                clean_text = self._remove_image_paths(content_text, image_files)
                if clean_text.strip():
                    content_parts.append({
                        "type": "text",
                        "text": clean_text.strip()
                    })

                if content_parts:
                    messages.append({
                        "role": role,
                        "content": content_parts
                    })
            else:
                # 纯文本内容
                if content_text.strip():
                    messages.append({
                        "role": role,
                        "content": content_text
                    })

        # 合并连续的相同角色消息
        merged_messages = self._merge_messages(messages)

        # 生成训练数据格式
        training_data = {
            "messages": merged_messages,
            "metadata": {
                "session_id": session.session_id,
                "command": session.command,
                "start_time": session.start_time,
                "end_time": session.end_time,
                "working_directory": session.working_directory,
                "exported_at": datetime.now().isoformat()
            }
        }

        output_path = export_dir / "session.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(training_data, f, ensure_ascii=False, indent=2)

        # 同时生成 JSONL 格式（每行一个对话，适合批量训练）
        jsonl_path = export_dir / "training.jsonl"
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({"messages": merged_messages}, ensure_ascii=False) + '\n')

        return output_path

    def _image_to_base64(self, image_path: str) -> Optional[str]:
        """将图片转换为 base64 data URL"""
        try:
            path = Path(image_path)
            if not path.exists():
                return None

            # 确定 MIME 类型
            suffix = path.suffix.lower()
            mime_types = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp'
            }
            mime_type = mime_types.get(suffix, 'image/png')

            # 读取并编码
            with open(path, 'rb') as f:
                img_data = f.read()
            b64_data = base64.b64encode(img_data).decode('utf-8')

            return f"data:{mime_type};base64,{b64_data}"
        except Exception:
            return None

    def _remove_image_paths(self, text: str, image_files: list) -> str:
        """从文本中移除图片路径"""
        result = text
        for img_path in image_files:
            # 移除完整路径
            result = result.replace(img_path, '')
            # 移除可能的文件名
            result = result.replace(Path(img_path).name, '')
        return result

    def _merge_messages(self, messages: list) -> list:
        """合并连续的相同角色消息"""
        if not messages:
            return messages

        merged = []
        for msg in messages:
            if not merged:
                merged.append(msg.copy())
                continue

            last = merged[-1]
            if last["role"] != msg["role"]:
                merged.append(msg.copy())
                continue

            # 相同角色，需要合并
            last_content = last["content"]
            curr_content = msg["content"]

            # 处理不同的 content 类型
            if isinstance(last_content, str) and isinstance(curr_content, str):
                # 两个都是纯文本
                last["content"] = last_content + "\n" + curr_content
            elif isinstance(last_content, list) and isinstance(curr_content, list):
                # 两个都是多模态
                last["content"].extend(curr_content)
            elif isinstance(last_content, str) and isinstance(curr_content, list):
                # 上一个是文本，当前是多模态
                last["content"] = [{"type": "text", "text": last_content}] + curr_content
            elif isinstance(last_content, list) and isinstance(curr_content, str):
                # 上一个是多模态，当前是文本
                last["content"].append({"type": "text", "text": curr_content})

        return merged

    def _filter_echo_entries(self, entries) -> list:
        """过滤掉终端回显的OUTPUT条目"""
        from utils import strip_ansi

        if not entries:
            return entries

        filtered = []
        i = 0
        while i < len(entries):
            entry = entries[i]

            # 如果是OUTPUT，检查后面是否紧跟着INPUT
            if entry.type == 'output' and i + 1 < len(entries):
                next_entry = entries[i + 1]
                if next_entry.type == 'input':
                    # 清理内容
                    output_clean = strip_ansi(entry.content)
                    input_clean = strip_ansi(next_entry.content).strip()

                    if input_clean:
                        # 检查OUTPUT是否包含INPUT的所有行（回显）
                        input_lines = [l.strip() for l in input_clean.split('\n') if l.strip()]
                        all_found = all(il in output_clean for il in input_lines)

                        if all_found:
                            # 这是回显，跳过
                            i += 1
                            continue

            filtered.append(entry)
            i += 1

        return filtered

    def _is_status_line(self, line: str) -> bool:
        """检测是否为需要过滤的UI行"""
        import re
        stripped = line.strip()

        if not stripped:
            return False

        # 输入提示行：以各种提示符开头（› > ❯ 〉等）
        prompt_prefixes = ['›', '>', '❯', '〉', '》']
        first_char = stripped[0] if stripped else ''
        if first_char in prompt_prefixes:
            return True

        # 以 └ 开头的行（树形结构提示）
        if stripped.startswith('└') or stripped.startswith('├'):
            return True

        # 图片选择提示
        if '[Image #' in stripped:
            return True

        # ctrl+c 提示
        if 'ctrl+c to interrupt' in stripped.lower():
            return True

        # 快捷键和UI提示
        ui_patterns = [
            r'^\?\s+for shortcuts',
            r'^[○●]\s*IDE',
            r'↵\s*send',
            r'↩\s*send',
            r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◑◒◓]\s+',
        ]
        for pattern in ui_patterns:
            if re.search(pattern, stripped):
                return True

        return False

    def _is_decoration_line(self, line: str) -> bool:
        """检测是否为纯装饰线（没有实际内容）"""
        stripped = line.strip()
        if not stripped:
            return False

        # 装饰字符
        deco_chars = set('─━═—–-_⎯┄┅┈┉ ')

        # 如果包含竖线或角落字符，说明是框架的一部分，保留
        frame_chars = set('│┃|╭╮╯╰┌┐├┤┬┴┼')
        if any(c in stripped for c in frame_chars):
            return False

        # 检查是否全部都是装饰字符
        return all(c in deco_chars for c in stripped)

    def _clean_whitespace(self, text: str) -> str:
        """清理多余的空白和重复内容"""
        import re
        from utils import strip_ansi

        lines = text.split('\n')
        cleaned_lines = []
        seen_content = set()  # 用于去重

        for line in lines:
            clean_line = strip_ansi(line)
            stripped = clean_line.strip()

            # 空行：只保留一个
            if stripped == '':
                if cleaned_lines and cleaned_lines[-1] != '':
                    cleaned_lines.append('')
                continue

            # 跳过UI元素行
            if self._is_status_line(clean_line):
                continue

            # 跳过纯装饰线
            if self._is_decoration_line(clean_line):
                continue

            # 跳过只有提示符的行（包括重复的提示符）
            prompt_only = stripped.replace('%', '').replace('$', '').replace('>', '').replace('›', '').replace('❯', '').strip()
            if not prompt_only:
                continue

            # 跳过只有特殊字符的行
            if stripped in ['└', '├', '|', '│']:
                continue

            # 去重：跳过已出现的相同内容
            if stripped in seen_content:
                continue
            seen_content.add(stripped)

            cleaned_lines.append(clean_line.rstrip())

        text = '\n'.join(cleaned_lines)
        text = re.sub(r'\n{2,}', '\n\n', text)
        text = text.strip()

        return text

    def _escape_html(self, text: str) -> str:
        """HTML转义"""
        return (text
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#39;'))

    def _colorize_diff(self, text: str) -> str:
        """为 diff 格式的内容添加颜色"""
        lines = text.split('\n')
        result = []

        for line in lines:
            # 跳过空行
            if not line:
                result.append(line)
                continue

            # 检测 diff 格式的行
            # 行号 + 或 行号 - 格式 (如 "6940 +" 或 "6947 -")
            import re
            # 匹配 "数字 +" 或 "数字 -" 或 "数字+" 或 "数字-"
            add_match = re.match(r'^(\d+\s*)\+(.*)$', line)
            del_match = re.match(r'^(\d+\s*)-(.*)$', line)

            if add_match:
                # 添加行 - 绿色
                result.append(f'<span class="diff-add">{line}</span>')
            elif del_match:
                # 删除行 - 红色
                result.append(f'<span class="diff-del">{line}</span>')
            else:
                result.append(line)

        return '\n'.join(result)

    def _ansi_to_html(self, text: str) -> str:
        """将ANSI颜色转换为HTML标签"""
        import re

        ansi_colors = {
            '30': 'ansi-black',
            '31': 'ansi-red',
            '32': 'ansi-green',
            '33': 'ansi-yellow',
            '34': 'ansi-blue',
            '35': 'ansi-magenta',
            '36': 'ansi-cyan',
            '37': 'ansi-white',
            # 亮色 (90-97)
            '90': 'ansi-black',
            '91': 'ansi-red',
            '92': 'ansi-green',
            '93': 'ansi-yellow',
            '94': 'ansi-blue',
            '95': 'ansi-magenta',
            '96': 'ansi-cyan',
            '97': 'ansi-white',
            '1': 'ansi-bold',
            '2': 'ansi-dim',
        }

        result = text
        # 简化处理：将常见ANSI序列转为span
        pattern = r'\x1b\[([0-9;]+)m'

        def replace_ansi(match):
            codes = match.group(1).split(';')
            classes = []
            for code in codes:
                if code in ansi_colors:
                    classes.append(ansi_colors[code])
                elif code == '0':
                    return '</span>'
            if classes:
                return f'<span class="{" ".join(classes)}">'
            return ''

        result = re.sub(pattern, replace_ansi, result)

        # 移除所有未处理的转义序列
        result = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', result)  # CSI序列
        result = re.sub(r'\x1b\][^\x07]*\x07', '', result)       # OSC序列
        result = re.sub(r'\x1b[()][AB012]', '', result)          # 字符集
        result = re.sub(r'\x1b[=>]', '', result)                 # 键盘模式
        result = re.sub(r'\r', '', result)                       # 回车符

        # 移除控制字符（保留换行和制表符）
        result = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', result)

        return result


def export_session(session: Session, format: str = 'html', open_after: bool = True) -> Path:
    """便捷导出函数"""
    exporter = Exporter()
    return exporter.export(session, format, open_after)

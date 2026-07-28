"""
终端屏幕层（从 terminal_widget.py 拆出，行为不变）

pyte 的扩展与纯函数 reflow 逻辑，不含任何 Qt 依赖：
- CompatibleHistoryScreen：pyte.HistoryScreen 兼容层 + 备用屏幕(1049/47/1047)
  + DECCKM/Bracketed Paste 追踪 + REP + resize 完整 reflow + 软换行追踪
- reflow_rows / map_reflow_position / _cell_is_blank：按新宽度重排的纯函数
- 模块导入即注册 pyte 缺失的 REP (CSI Pn b) 分发

terminal_widget 对外再导出上述符号，既有 import 路径不变。
"""
import bisect

import pyte
from pyte.screens import StaticDefaultDict, wcwidth


# pyte 0.8 未实现 REP (CSI Pn b) —— 把前一个图形字符重复 N 次。
# nvidia-smi / watch / 部分现代 CLI 会用 "─\x1b[60b" 这种方式画横线，
# 不注册这个分发会导致整段重复被静默丢弃（表格横线只剩一两格）。
# pyte.Stream 在实例化时就把 csi 表捕获进 dispatcher 协程，所以必须
# 在任何 Stream 创建之前就往类属性里写。
if 'b' not in pyte.Stream.csi:
    pyte.Stream.csi['b'] = 'repeat'


def _cell_is_blank(ch) -> bool:
    """单元格在视觉上是否等同于「空」（可被逻辑行尾部裁剪）。

    带背景色 / 反显 / 下划线 / 删除线的空格是可见内容（如 TUI 的色块），
    不能当作空白裁掉。
    """
    data = ch.data
    return ((data == ' ' or data == '') and ch.bg == 'default'
            and not ch.reverse and not ch.underscore and not ch.strikethrough)


def reflow_rows(rows, soft_flags, new_columns, old_columns=None):
    """把物理行按软换行标记拼成逻辑行，再按 new_columns 重新折行（纯函数）。

    输入格式：
        rows        —— 物理行列表；每行是按列号升序的 [(col, Char), ...]，
                       或等价的稀疏 dict {col: Char}（按需惰性转换，调用方可
                       直接传入 pyte 的行对象，免去为快路径行做提取的开销）；
                       行内可有空洞；宽字符占两格：本体 Char 在 col，
                       stub（data == ''）在 col+1（行尾放不下 stub 时可缺失）。
        soft_flags  —— soft_flags[i] 为 True 表示第 i 行与下一行同属一个逻辑行
                       （终端 DECAWM 自动换行）。
        new_columns —— 新的终端列宽。
        old_columns —— 旧列宽；用于把软换行行的尾部空洞补齐到整行宽度，
                       保持逻辑行内的相对位置（None 时不补）。

    返回 (new_rows, new_soft_flags, row_map)：
        new_rows / new_soft_flags —— 新物理行（[(col, Char), ...] 格式）与软换行
            标记；未受影响的行（单行逻辑行且宽度放得下）原样返回**同一个对象**
            （列表或 dict，与传入一致），调用方可借对象身份复用原行容器（性能关键）。
        row_map —— (old_starts, new_starts)，均为与行列表平行的
            [(logical_idx, start_offset), ...]，start_offset 是该物理行行首
            在逻辑行内的列偏移；配合 map_reflow_position 做光标重定位。

    规则：
        · 宽字符不跨行切断：行尾只剩 1 列放不下时整体挪到下一行；
        · 全空逻辑行保留为一个空物理行（段落间空行不丢）；
        · 逻辑行尾部的空白单元格（_cell_is_blank）与空洞被裁剪，
          避免被擦除产生的整行尾部空格在变窄时折出多余空行。
    """
    # 列宽不变（纯行数变化）：折行布局完全不变，整体直通（行数迁移由调用方
    # 在屏幕/历史之间重新分配，这里只需给出恒等映射）
    if old_columns is not None and old_columns == new_columns:
        starts = []
        li = 0
        off = 0
        for idx, flag in enumerate(soft_flags):
            starts.append((li, off))
            if flag and idx < len(rows) - 1:
                off += old_columns
            else:
                li += 1
                off = 0
        return list(rows), list(soft_flags), (starts, starts)

    out_rows, out_soft = [], []
    old_starts, new_starts = [], []
    wcw = wcwidth  # 局部绑定（热路径）
    li = 0  # 逻辑行序号
    i, n = 0, len(rows)
    while i < n:
        # 找到当前逻辑行覆盖的物理行区间 [i, j]
        j = i
        while j < n - 1 and soft_flags[j]:
            j += 1

        # ---- 快路径：单行逻辑行且新宽度放得下 → 原样透传（零拷贝/零提取）----
        if i == j:
            row = rows[i]
            if not row:
                fits = True
            else:
                if type(row) is list:
                    last_col, last_ch = row[-1]
                else:
                    last_col = max(row)
                    last_ch = row[last_col]
                data = last_ch.data
                width = 2 if (data and wcw(data[0]) == 2) else 1
                fits = last_col + width <= new_columns
            if fits:
                old_starts.append((li, 0))
                new_starts.append((li, 0))
                out_rows.append(row)
                out_soft.append(False)
                li += 1
                i = j + 1
                continue

        # ---- 慢路径：惰性转换为有序单元格列表 ----
        conv = [r if type(r) is list else sorted(r.items())
                for r in (rows[k] for k in range(i, j + 1))]

        # 定位逻辑行尾部空白裁剪点（最后一个「有意义」单元格的 (行, 行内下标)）
        k_last, idx_last = -1, -1
        for k in range(j - i, -1, -1):
            cells = conv[k]
            for idx in range(len(cells) - 1, -1, -1):
                if not _cell_is_blank(cells[idx][1]):
                    k_last, idx_last = k, idx
                    break
            if k_last >= 0:
                break

        if k_last < 0:
            # 全空逻辑行 → 保留一个空物理行（段落间空行不丢）
            line_off = 0
            for k in range(i, j + 1):
                old_starts.append((li, line_off))
                line_off += old_columns if old_columns is not None else 0
            out_rows.append([])
            out_soft.append(False)
            new_starts.append((li, 0))
            li += 1
            i = j + 1
            continue

        # ---- 子快路径：稠密、纯 ASCII 的逻辑行（被折行的长 ASCII 行最常见）----
        # 拼接 Char 引用列表后按宽度切片，单元格由 C 级 enumerate 一次性产出，
        # 避免逐格 Python 循环（性能关键：20000 行历史 reflow 的主要开销在这里）。
        # 宽字符/组合字符/stub（''）经 join+isascii/长度校验排除，转入通用路径。
        dense = True
        for k in range(k_last + 1):
            cells = conv[k]
            m = len(cells)
            if (not cells or cells[0][0] != 0 or cells[-1][0] != m - 1
                    or (k < k_last and old_columns is not None
                        and m != old_columns)):
                dense = False
                break
            # data 是 Char（namedtuple）的第 0 个字段，c[1][0] 为 C 级索引访问；
            # join 后整体 isascii + 长度比对：'' stub（长度短缺）与多字符组合
            # 数据（长度超出）都会失配
            datas = ''.join([c[1][0] for c in cells])
            if len(datas) != m or not datas.isascii():
                dense = False
                break
        if dense:
            chars = []
            line_off = 0
            for k in range(j - i + 1):
                old_starts.append((li, line_off))
                if k > k_last:
                    line_off += old_columns if old_columns is not None else 0
                    continue
                cells = conv[k]
                if k == k_last:
                    cells = cells[:idx_last + 1]
                chars += [c[1] for c in cells]
                line_off = len(chars)
            total_len = len(chars)
            s = 0
            while s < total_len:
                out_rows.append(list(enumerate(chars[s:s + new_columns])))
                new_starts.append((li, s))
                s += new_columns
                out_soft.append(s < total_len)
            li += 1
            i = j + 1
            continue

        # ---- 通用路径（单遍融合）：边拼接边按 new_columns 折行 ----
        # Char 是 immutable namedtuple，全程引用复用，不逐字符新建对象。
        cur = []
        cur_append = cur.append
        ccol = 0       # 当前新行内的列位置
        off = 0        # 逻辑行内的列偏移
        row_start = 0  # 当前新行行首的逻辑偏移
        line_off = 0   # 当前物理行行首在逻辑行内的列偏移
        for k in range(j - i + 1):
            old_starts.append((li, line_off))
            if k > k_last:
                # 裁剪点之后的纯空白行：只占位（光标映射用），不产出内容
                line_off += old_columns if old_columns is not None else 0
                continue
            cells = conv[k]
            m = idx_last + 1 if k == k_last else len(cells)
            pos = 0
            idx = 0
            while idx < m:
                col, ch = cells[idx]
                idx += 1
                if col > pos:
                    # 行内空洞：不产生单元格，只推进列位置，可跨行切分
                    rem = col - pos
                    pos = col
                    while rem:
                        space = new_columns - ccol
                        if space <= 0:
                            out_rows.append(cur)
                            out_soft.append(True)
                            new_starts.append((li, row_start))
                            cur = []
                            cur_append = cur.append
                            ccol = 0
                            row_start = off
                            space = new_columns
                        take = rem if rem < space else space
                        ccol += take
                        off += take
                        rem -= take
                data = ch[0]  # Char 是 namedtuple，data 是第 0 个字段（C 级索引）
                if data and wcw(data[0]) == 2:
                    stub = None
                    if idx < m and cells[idx][0] == col + 1 \
                            and cells[idx][1][0] == '':
                        stub = cells[idx][1]
                        idx += 1
                    if ccol + 2 > new_columns and ccol > 0:
                        # 宽字符放不下（行尾只剩 1 列）→ 整体挪到下一行，不切断
                        out_rows.append(cur)
                        out_soft.append(True)
                        new_starts.append((li, row_start))
                        cur = []
                        cur_append = cur.append
                        ccol = 0
                        row_start = off
                    cur_append((ccol, ch))
                    if ccol + 1 < new_columns:
                        cur_append((ccol + 1, stub if stub is not None
                                    else ch._replace(data='')))
                    ccol += 2
                    off += 2
                    pos = col + 2
                else:
                    if ccol >= new_columns:
                        out_rows.append(cur)
                        out_soft.append(True)
                        new_starts.append((li, row_start))
                        cur = []
                        cur_append = cur.append
                        ccol = 0
                        row_start = off
                    cur_append((ccol, ch))
                    ccol += 1
                    off += 1
                    pos = col + 1
            # 软换行行（非裁剪点行）尾部空洞补齐到旧整行宽度，保持行内相对位置。
            # 例外：尾部恰空 1 列且下一行行首是宽字符 —— 这是「宽字符放不下被整体
            # 挪到下一行」留下的折行痕迹，不是内容，补进去会让拉宽后的行多出空格。
            if k < k_last and old_columns is not None and pos < old_columns:
                gap = old_columns - pos
                if gap == 1:
                    nxt_cells = conv[k + 1]
                    if nxt_cells:
                        c0, ch0 = nxt_cells[0]
                        d0 = ch0.data
                        if c0 == 0 and d0 and wcw(d0[0]) == 2:
                            gap = 0
                if gap:
                    pos += gap
                    rem = gap
                    while rem:
                        space = new_columns - ccol
                        if space <= 0:
                            out_rows.append(cur)
                            out_soft.append(True)
                            new_starts.append((li, row_start))
                            cur = []
                            cur_append = cur.append
                            ccol = 0
                            row_start = off
                            space = new_columns
                        take = rem if rem < space else space
                        ccol += take
                        off += take
                        rem -= take
            line_off += pos
        out_rows.append(cur)
        out_soft.append(False)
        new_starts.append((li, row_start))
        li += 1
        i = j + 1

    return out_rows, out_soft, (old_starts, new_starts)


def map_reflow_position(row_map, old_row, old_col):
    """把 reflow 前的 (物理行号, 列号) 映射到 reflow 后的 (物理行号, 列号)。

    row_map 来自 reflow_rows 的第三个返回值。返回的列号未做 clamp
    （光标可能停在被裁剪的尾部空白之后），调用方按需夹紧。
    """
    old_starts, new_starts = row_map
    if not old_starts or not new_starts:
        return 0, 0
    if old_row >= len(old_starts):
        old_row = len(old_starts) - 1
    li, start = old_starts[old_row]
    off = start + (old_col if old_col > 0 else 0)
    # 找同一逻辑行内 start_offset <= off 的最后一个新行
    idx = bisect.bisect_right(new_starts, (li, off)) - 1
    if idx < 0:
        idx = 0
    nli, ns = new_starts[idx]
    if nli != li:
        # 防御：不应发生（每个逻辑行至少产出一行）；回退到该逻辑行首行
        idx = min(bisect.bisect_left(new_starts, (li, 0)), len(new_starts) - 1)
        ns = new_starts[idx][1]
    return idx, off - ns


class CompatibleHistoryScreen(pyte.HistoryScreen):
    """兼容性修复：处理新版 pyte 传递的 private 参数
    并实现备用屏幕缓冲区（mode 1049/47/1047），pyte 0.8 原生不支持。
    """

    # 备用屏幕相关的私有模式号
    _ALT_SCREEN_MODES = frozenset({47, 1047, 1049})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 累计推入历史的总行数（用于滚动位置稳定化）
        self._total_history_lines = 0
        # 软换行追踪：存储自动换行的行对象 id
        self._soft_wrapped_ids = set()
        self._in_draw = False

        # 备用屏幕缓冲区支持
        self._in_alt_screen = False
        self._saved_main_buffer = None
        self._saved_main_cursor = None
        self._saved_main_history_lines = 0

        # DECCKM (Application Cursor Keys) 模式追踪
        self._decckm = False

        # Bracketed Paste (mode 2004) 模式追踪
        self._bracketed_paste = False

        # REP (CSI Pn b) 需要记住最近一次 draw 的图形字符
        self._last_drawn_char = None

    def select_graphic_rendition(self, *attrs, **kwargs):
        # 移除 private 参数（新版 pyte 会传递，但基类不支持）
        kwargs.pop('private', None)
        return super().select_graphic_rendition(*attrs, **kwargs)

    # ------ 备用屏幕缓冲区 ------

    def set_mode(self, *modes, **kwargs):
        """拦截 private mode 设置，处理备用屏幕切换和 DECCKM"""
        if kwargs.get('private'):
            if not self._in_alt_screen and (self._ALT_SCREEN_MODES & set(modes)):
                self._enter_alt_screen(save_cursor=(1049 in modes))
            # DECCKM (mode 1): 应用光标键模式
            if 1 in modes:
                self._decckm = True
            # Bracketed Paste (mode 2004)
            if 2004 in modes:
                self._bracketed_paste = True
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        """拦截 private mode 重置，处理备用屏幕退出和 DECCKM"""
        if kwargs.get('private'):
            if self._in_alt_screen and (self._ALT_SCREEN_MODES & set(modes)):
                self._leave_alt_screen(restore_cursor=(1049 in modes))
            # DECCKM (mode 1): 关闭应用光标键模式
            if 1 in modes:
                self._decckm = False
            # Bracketed Paste (mode 2004)
            if 2004 in modes:
                self._bracketed_paste = False
        super().reset_mode(*modes, **kwargs)

    def _enter_alt_screen(self, save_cursor=True):
        """进入备用屏幕：保存主缓冲区，创建空的备用缓冲区"""
        import copy
        # 保存主屏幕的缓冲区和光标。
        # pyte 的 Char 是 immutable namedtuple，行级浅拷贝即可完全隔离，
        # 无需 deepcopy（deepcopy 会递归复制每个 Char，大屏幕下非常慢）。
        # copy.copy 能保留容器类型及其默认值行为：
        # - 外层 defaultdict 保留 default_factory
        # - 每行 StaticDefaultDict 保留 .default（缺失列返回 default_char）
        saved = copy.copy(self.buffer)
        sw = self._soft_wrapped_ids
        for row_idx in list(saved):
            orig = saved[row_idx]
            row_copy = copy.copy(orig)
            saved[row_idx] = row_copy
            # 软换行标记按对象 id 登记：行被拷贝后 id 变化，必须把标记迁移到
            # 拷贝上（原行对象随后被 clear 用作备用屏幕行，不应再带主屏标记），
            # 否则备用屏幕期间 resize/退出后主屏的软换行信息丢失，reflow 失效。
            if id(orig) in sw:
                sw.discard(id(orig))
                sw.add(id(row_copy))
        self._saved_main_buffer = saved
        self._saved_main_history_lines = self._total_history_lines
        if save_cursor:
            self._saved_main_cursor = copy.copy(self.cursor)
        self._in_alt_screen = True
        # 清空当前缓冲区作为备用屏幕
        for row in range(self.lines):
            self.buffer[row].clear()
        self.cursor.x = 0
        self.cursor.y = 0

    def _leave_alt_screen(self, restore_cursor=True):
        """退出备用屏幕：恢复主缓冲区"""
        if self._saved_main_buffer is not None:
            # 备用屏幕行对象即将废弃：清掉它们的软换行标记，避免 id 复用造成误判
            sw = self._soft_wrapped_ids
            for row_idx in list(self.buffer):
                sw.discard(id(self.buffer[row_idx]))
            self.buffer = self._saved_main_buffer
            self._total_history_lines = self._saved_main_history_lines
            if restore_cursor and self._saved_main_cursor is not None:
                self.cursor.x = self._saved_main_cursor.x
                self.cursor.y = self._saved_main_cursor.y
            # 防御：备用屏幕期间若发生过 resize，光标可能落在新屏幕之外，强制夹紧
            self.cursor.y = max(0, min(self.cursor.y, self.lines - 1))
            self.cursor.x = max(0, min(self.cursor.x, self.columns - 1))
            self._saved_main_buffer = None
            self._saved_main_cursor = None
        self._in_alt_screen = False

    def resize(self, lines=None, columns=None):
        """重写 resize：完整 reflow（重排），对齐 iTerm2 行为。

        pyte 原生 resize 在列数变小时 line.pop(x) 永久删除超宽内容（拉宽后丢失），
        行数变化时只在底部加空行/从顶部删行、不与历史交互。这里改为：

        · 主屏路径：history.top + 屏幕缓冲区按软换行标记拼成逻辑行，按新宽度
          重新折行；行数变化时内容在屏幕与历史之间迁移（变矮把顶部行推入历史，
          变高从历史拉回）；光标跟随其逻辑位置。
        · 备用屏幕路径：备用屏幕本身不 reflow（TUI 收到 SIGWINCH 会自己整屏
          重画），沿用 pyte 原生 resize；但被保存的主屏缓冲区 + 历史按上述逻辑
          重排，退出 TUI 时才能按新宽度恢复完整内容。
        """
        new_lines = lines if lines is not None else self.lines
        new_columns = columns if columns is not None else self.columns
        if new_lines == self.lines and new_columns == self.columns:
            return

        if self._in_alt_screen:
            if self._saved_main_buffer is not None:
                self._reflow_main_screen(new_lines, new_columns,
                                         self._saved_main_buffer,
                                         self._saved_main_cursor)
                self._saved_main_history_lines = self._total_history_lines
            super().resize(new_lines, new_columns)
            return

        self._reflow_main_screen(new_lines, new_columns, self.buffer, self.cursor)
        self.lines = new_lines
        self.columns = new_columns
        self.dirty.update(range(new_lines))
        # 与 pyte 原生 resize 一致：滚动边距复位为整屏
        self.set_margins()

    def _reflow_main_screen(self, new_lines, new_columns, buffer, cursor):
        """按新尺寸重排主屏状态：history.top + buffer + 软换行标记 + 光标。

        buffer/cursor 既可以是当前活动的主屏状态（普通 resize），也可以是进入
        备用屏幕时保存的 _saved_main_buffer/_saved_main_cursor（TUI 期间 resize，
        此时 history 同样属于主屏，一并重排）。本方法不修改 self.lines/columns。
        """
        old_lines, old_columns = self.lines, self.columns
        history = self.history

        # history.bottom（向下分页缓冲）：本应用从不使用 pyte 的 prev/next_page，
        # 正常情况下恒空。防御：若非空，视为「屏幕下方的内容」并入重排，
        # 并把分页位置归位到底部。
        bottom_rows = list(history.bottom)
        if bottom_rows:
            history.bottom.clear()
            self.history = history = history._replace(position=history.size)

        htop = list(history.top)

        # 屏幕缓冲区只取到「最后有内容的行」与光标行的较大者，光标以下的纯空行
        # 丢弃（否则变矮时这些空行会把真实内容顶进历史）。
        last_content = -1
        for y in range(old_lines):
            line = buffer.get(y)
            if line and any(not _cell_is_blank(c) for c in line.values()):
                last_content = y
        keep = last_content + 1
        if cursor is not None:
            keep = max(keep, min(cursor.y, old_lines - 1) + 1)

        src_lines = htop + [buffer.get(y) for y in range(keep)] + bottom_rows

        # 行对象（稀疏 dict）直接传给纯函数（按需惰性转换，免去为快路径行做
        # 提取的开销）；快路径透传会原样返回同一对象，借 id 复用原行容器。
        sw = self._soft_wrapped_ids
        rows = [line if line else [] for line in src_lines]
        flags = [line is not None and id(line) in sw for line in src_lines]
        reuse = {id(line): line for line in src_lines if line}

        new_rows, new_soft, row_map = reflow_rows(
            rows, flags, new_columns, old_columns)

        # 重建行对象（未受影响的行复用原对象）
        default_char = self.default_char
        line_objs = []
        for cells in new_rows:
            orig = reuse.get(id(cells))
            if orig is not None:
                line_objs.append(orig)
            else:
                line_obj = StaticDefaultDict(default_char)
                if cells:
                    line_obj.update(cells)
                line_objs.append(line_obj)

        # 重新分配：尾部 new_lines 行进屏幕，其余进历史（变矮推入历史/变高拉回）
        total = len(line_objs)
        screen_n = min(total, new_lines)
        hist_n = total - screen_n

        old_hist_len = len(htop)
        history.top.clear()
        history.top.extend(line_objs[:hist_n])  # deque maxlen 自动丢弃最老的行
        # 累计计数按实际历史行数差额修正，保证渲染端滚动锚点（按差值补偿）不跳
        self._total_history_lines += len(history.top) - old_hist_len

        buffer.clear()
        for k in range(screen_n):
            buffer[k] = line_objs[hist_n + k]

        # 光标按逻辑位置重定位，clamp 到新屏幕内（x 允许 == columns：
        # 与 pyte draw 写满整行后的「待换行」状态一致）
        if cursor is not None and keep > 0:
            cy = min(max(cursor.y, 0), keep - 1)
            new_idx, new_x = map_reflow_position(
                row_map, old_hist_len + cy, cursor.x)
            cursor.y = max(0, min(new_idx - hist_n, new_lines - 1))
            cursor.x = max(0, min(new_x, new_columns))

        # 软换行标记重建为新行对象的 id（顺带清掉已死行的陈旧 id）；
        # 备用屏幕期间 resize 时保留 alt 缓冲区自身行的标记
        new_ids = set()
        for line_obj, flag in zip(line_objs, new_soft):
            if flag:
                new_ids.add(id(line_obj))
        if self._in_alt_screen:
            for y in range(old_lines):
                line = self.buffer.get(y)
                if line is not None and id(line) in sw:
                    new_ids.add(id(line))
        self._soft_wrapped_ids = new_ids

    # ------ 原有功能 ------

    def draw(self, *chars):
        self._in_draw = True
        try:
            super().draw(*chars)
            # 记录最后绘制的字符，供 REP (CSI Pn b) 使用
            for piece in chars:
                if piece:
                    self._last_drawn_char = piece[-1] if isinstance(piece, str) else piece
        finally:
            self._in_draw = False

    def repeat(self, count=1, *args, **kwargs):
        """REP (CSI Pn b): 将上一个图形字符重复 N 次。"""
        if self._last_drawn_char is None:
            return
        try:
            n = int(count) if count else 1
        except (TypeError, ValueError):
            n = 1
        # 防御性上限：避免恶意/错误序列写爆一整行
        n = max(1, min(n, self.columns))
        self.draw(self._last_drawn_char * n)

    def linefeed(self):
        if not self._in_draw:
            # 显式换行(\n)：确保当前行不被标记为软换行
            self._soft_wrapped_ids.discard(id(self.buffer[self.cursor.y]))
        super().linefeed()

    def index(self):
        """重写以追踪推入历史的行数和软换行状态"""
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom and not self._in_alt_screen:
            # 备用屏幕上不追踪主屏幕的历史行数
            self._total_history_lines += 1
        if self._in_draw:
            # draw() 触发的 index 是自动换行（软换行）
            self._soft_wrapped_ids.add(id(self.buffer[self.cursor.y]))
        if self._in_alt_screen:
            # 备用屏幕上不推入主屏幕历史，直接调用 Screen.index
            pyte.Screen.index(self)
        else:
            super().index()

    def erase_in_display(self, how=0, *args, **kwargs):
        # 清屏时清理软换行标记。只清当前缓冲区行的标记：清屏不影响历史行，
        # 备用屏幕上的清屏（TUI 启动时的 \x1b[2J 很常见）更不能动主屏/历史的
        # 标记，否则之后 resize reflow 时无法把被折行的主屏长行拼回逻辑行。
        if how == 2 or how == 3:
            sw = self._soft_wrapped_ids
            for y in range(self.lines):
                line = self.buffer.get(y)
                if line is not None:
                    sw.discard(id(line))
        super().erase_in_display(how, *args, **kwargs)

    def reset(self):
        # reset() 可能在 __init__ 中被 super().__init__() 调用
        if hasattr(self, '_soft_wrapped_ids'):
            self._soft_wrapped_ids.clear()
            self._in_draw = False
        if hasattr(self, '_in_alt_screen') and self._in_alt_screen:
            self._in_alt_screen = False
            self._saved_main_buffer = None
            self._saved_main_cursor = None
        if hasattr(self, '_last_drawn_char'):
            self._last_drawn_char = None
        super().reset()

    def clear_scrollback(self):
        """清空回滚历史(scrollback),保留当前可见屏幕。

        只清 history.top/bottom 与历史计数,不动 self.buffer(可见区),所以
        屏幕上看到的内容不变,只是上方可回滚的历史被丢弃、内存随之释放。
        对运行中的终端同样有效(与构造时定死的 maxlen 上限互补)。
        """
        history = getattr(self, 'history', None)
        if history is None:
            return
        history.top.clear()
        history.bottom.clear()
        # position 复位到底部,避免分页状态残留(top/bottom 已被原地清空)
        self.history = history._replace(position=history.size)
        self._total_history_lines = 0

    def is_soft_wrapped(self, buffer_line) -> bool:
        """检查指定行是否因自动换行而换行"""
        return id(buffer_line) in self._soft_wrapped_ids

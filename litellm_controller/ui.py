"""InquirerPy 扩展：仿 checkbox 样式的 fuzzy 变体、fuzzy 专用不可选行、上下固定区域。

说明（基于 InquirerPy 0.3.4 源码）：
- 官方 Separator 在 fuzzy 中会抛 InvalidArgument，故用 FuzzySeparator sentinel 替代；
- fuzzy 的导航默认不跳过任何条目，且回车无 enabled 检查，
  故 DotFuzzy 重写 _handle_up/_handle_down/_handle_enter 跳过 FuzzySeparator 行
  （与官方 ListPrompt 跳过 Separator 的模式一致），多选全选(ctrl+a)同样跳过；
- 渲染方法（_get_hover_text/_get_normal_text）与 pointer/marker 状态都在
  InquirerPyFuzzyControl 上，故在实例上直接替换/覆盖；
- 符号格式（单选/多选统一）：❯ 光标、◉ 选中、○ 未选中，符号后带空格；
  FuzzySeparator 行按官方 Separator 方式渲染（class:separator，无符号前缀）；
  DisabledChoice 行以灰色（DISABLED_STYLE）渲染，与 FuzzySeparator 一样不可选
  （↑↓/Home/End 跳过、回车不响应、多选不可勾选），用于"列出但不可选"的选项
  （如已归属其他路由组的模型）；
- PinnedFuzzyControl 重写渲染：
  * head = 开头连续的 pinned FuzzySeparator（顶框线/表头），始终显示在顶部；
  * tail = 最后一个 pinned FuzzySeparator 及其后的选项（底框线+操作选项），
    始终显示在底部，且以普通 list 样式渲染（无 ●/○ 标记）；
  * 中间数据区在滚动窗口内展示，窗口高度扣减 head/tail 行数。
"""
import InquirerPy.prompts.fuzzy as _fuzzy_mod
from InquirerPy.base.list import BaseListPrompt
from InquirerPy.prompts.fuzzy import FuzzyPrompt, InquirerPyFuzzyControl
from InquirerPy.resolver import question_mapping


class FuzzySeparator:
    """fuzzy 题型中永不可选的行（框线/表头/信息行）。勿用 InquirerPy.separator.Separator。

    indent=True 时行首补 4 个空格，与数据行的 "❯ ◉ " 前缀对齐（表头用）。
    pinned=True 的行参与 head/tail 固定；内容型信息行（如已添加模型列表）用 pinned=False，
    使其随数据区滚动、参与筛选。
    """

    def __init__(self, line: str = "─" * 80, indent: bool = False, pinned: bool = True):
        self._line = line
        self._indent = indent
        self.pinned = pinned

    def __str__(self) -> str:
        return "    " + self._line if self._indent else self._line


# 不可选行的样式（内联 style，不依赖主题注册）
DISABLED_STYLE = "dim"


class DisabledChoice:
    """fuzzy 题型中"列出但不可选"的选项：灰色显示，光标经过时跳过。

    通过 Choice(DisabledChoice(value, reason), name=显示文本) 传入 prompt。
    ↑↓/Home/End/PgUp/PgDn 均跳过该行，回车不响应，多选不可勾选、全选跳过。
    reason 仅备查（显示文本由 Choice.name 控制）。
    """

    def __init__(self, value, reason: str = ""):
        self.value = value
        self.reason = reason

    def __str__(self) -> str:
        return str(self.value)


class PinnedFuzzyControl(InquirerPyFuzzyControl):
    """fuzzy 控件变体：顶部 head 与底部 tail 固定显示，中间数据区滚动。"""

    def _head_end(self) -> int:
        n = 0
        for c in self._filtered_choices:
            v = c["value"]
            if isinstance(v, FuzzySeparator) and v.pinned:
                n += 1
            else:
                break
        return n

    def _tail_start(self) -> int:
        filtered = self._filtered_choices
        for i in range(len(filtered) - 1, -1, -1):
            v = filtered[i]["value"]
            if isinstance(v, FuzzySeparator) and v.pinned:
                return i
        return len(filtered)

    def _plain_text(self, choice, hover: bool):
        """tail 区渲染：普通 list 样式（无 ◉/○ 标记），分隔线用 class:separator。"""
        if isinstance(choice["value"], FuzzySeparator):
            return [("class:separator", str(choice["value"]))]
        if isinstance(choice["value"], DisabledChoice):
            return [(DISABLED_STYLE, choice["name"])]
        base = InquirerPyFuzzyControl._get_hover_text if hover else InquirerPyFuzzyControl._get_normal_text
        text = base(self, choice)
        del text[1]
        return text

    def _get_formatted_choices(self):
        display_choices = []
        if self.choice_count == 0:
            self._selected_choice_index = 0
            self._first_line = 0
            self._last_line = 0
            return display_choices

        if self._selected_choice_index < 0:
            self._selected_choice_index = 0
        elif self._selected_choice_index >= self.choice_count:
            self._selected_choice_index = self.choice_count - 1

        head_end = self._head_end()
        tail_start = self._tail_start()
        if head_end > tail_start:
            head_end = tail_start
        data_count = tail_start - head_end
        tail_count = self.choice_count - tail_start
        avail = max(self._height - head_end - tail_count, 0)

        sel = self._selected_choice_index
        if data_count > 0:
            if sel < head_end:
                lf, lh = 0, min(avail, data_count)
            elif sel >= tail_start:
                lh = data_count
                lf = max(lh - min(avail, data_count), 0)
            else:
                s = sel - head_end
                lh = max(s + 1, min(avail, data_count))
                lf = max(lh - min(avail, data_count), 0)
            self._first_line = head_end + lf
            self._last_line = head_end + lh
        else:
            self._first_line = head_end
            self._last_line = head_end

        for index in range(head_end):
            display_choices += self._get_normal_text(self._filtered_choices[index])
            display_choices.append(("", "\n"))
        for index in range(self._first_line, self._last_line):
            choice = self._filtered_choices[index]
            if index == self.selected_choice_index:
                display_choices += self._get_hover_text(choice)
            else:
                display_choices += self._get_normal_text(choice)
            display_choices.append(("", "\n"))
        for index in range(tail_start, self.choice_count):
            choice = self._filtered_choices[index]
            display_choices += self._plain_text(choice, index == self.selected_choice_index)
            display_choices.append(("", "\n"))
        if display_choices:
            display_choices.pop()
        return display_choices


class DotFuzzy(FuzzyPrompt):
    POINTER = "❯ "
    DOT_ON = "◉ "
    DOT_OFF = "○ "

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_data_index = None
        # Tab/Shift+Tab：在表格（数据区）与底部操作栏（tail）间跳转光标。
        # 覆盖默认 toggle-down/toggle-up（原本 Tab=下移/勾选+下移，多选勾选已改用空格）。
        self.kb_func_lookup = {
            "toggle-down": [{"func": self._handle_tab}],
            "toggle-up": [{"func": self._handle_tab}],
        }
        self.register_kb("pagedown")(self._handle_page_down)
        self.register_kb("pageup")(self._handle_page_up)
        self.register_kb("home")(self._handle_home)
        self.register_kb("end")(self._handle_end)
        ctrl = self._content_control
        ctrl._pointer = self.POINTER
        if self._multiselect:
            if not kwargs.get("marker"):
                ctrl._marker = self.DOT_ON
            if not kwargs.get("marker_pl"):
                ctrl._marker_pl = self.DOT_OFF
        else:
            first = next(
                (
                    i
                    for i, c in enumerate(ctrl.choices)
                    if not isinstance(c["value"], (FuzzySeparator, DisabledChoice))
                ),
                0,
            )
            ctrl.selected_choice_index = first
        base_hover = InquirerPyFuzzyControl._get_hover_text
        base_normal = InquirerPyFuzzyControl._get_normal_text
        ctrl._get_hover_text = lambda choice: self._dot_text(base_hover, ctrl, choice, self.DOT_ON)
        ctrl._get_normal_text = lambda choice: self._dot_text(base_normal, ctrl, choice, self.DOT_OFF)

    def _dot_text(self, base, ctrl, choice, dot: str):
        v = choice["value"]
        if isinstance(v, FuzzySeparator):
            return [("class:separator", str(v))]
        if isinstance(v, DisabledChoice):
            # 灰色行：前缀占位（空格+○）保持与数据行 "❯ ◉ " 同宽
            return [(DISABLED_STYLE, f"  {self.DOT_OFF}{choice['name']}")]
        text = base(ctrl, choice)
        if not self._multiselect:
            text[1] = ("class:marker", dot)
        return text

    def _is_non_selectable(self, choice: dict) -> bool:
        return isinstance(choice["value"], (FuzzySeparator, DisabledChoice))

    def _skip_separator(self, direction: int, event) -> None:
        count = self.content_control.choice_count
        for _ in range(count):
            if direction > 0:
                BaseListPrompt._handle_down(self, event)
            else:
                BaseListPrompt._handle_up(self, event)
            try:
                selection = self.content_control.selection
            except IndexError:
                break
            if not self._is_non_selectable(selection):
                return

    def _handle_down(self, event) -> None:
        self._skip_separator(+1, event)

    def _handle_up(self, event) -> None:
        self._skip_separator(-1, event)

    def _data_indices(self) -> list:
        """数据区（head 与 tail 之间）内可选条目在 _filtered_choices 中的索引。"""
        ctrl = self.content_control
        filtered = ctrl._filtered_choices
        if not filtered:
            return []
        head_end = ctrl._head_end()
        tail_start = ctrl._tail_start()
        if head_end > tail_start:
            head_end = tail_start
        return [
            i
            for i in range(head_end, tail_start)
            if not isinstance(filtered[i]["value"], (FuzzySeparator, DisabledChoice))
        ]

    def _tail_indices(self) -> list:
        """tail 区（末尾 pinned 分隔线之后）内可选条目在 _filtered_choices 中的索引。"""
        ctrl = self.content_control
        filtered = ctrl._filtered_choices
        if not filtered:
            return []
        tail_start = ctrl._tail_start()
        return [
            i
            for i in range(tail_start, len(filtered))
            if not isinstance(filtered[i]["value"], (FuzzySeparator, DisabledChoice))
        ]

    def _handle_tab(self, event) -> None:
        """Tab/Shift+Tab：在表格（数据区）与底部操作栏（tail）间跳转光标。

        - 光标在数据区 → 跳到 tail 第一个操作，并记录当前数据位置；
        - 光标在 tail → 回到最近的数据位置（无有效记录则取数据区首项）。
        目标区域为空时不移动。
        """
        ctrl = self.content_control
        sel = ctrl.selected_choice_index
        data_idx = self._data_indices()
        tail_idx = self._tail_indices()
        if not data_idx and not tail_idx:
            return
        if sel in tail_idx:
            if self._last_data_index in data_idx:
                target = self._last_data_index
            elif data_idx:
                target = data_idx[0]
            else:
                return
        else:
            if sel in data_idx:
                self._last_data_index = sel
            if not tail_idx:
                return
            target = tail_idx[0]
        ctrl.selected_choice_index = target

    def _handle_page_down(self, event) -> None:
        self._move_by_page(+1)

    def _handle_page_up(self, event) -> None:
        self._move_by_page(-1)

    def _move_by_page(self, direction: int) -> None:
        ctrl = self.content_control
        filtered = ctrl._filtered_choices
        data_idx = self._data_indices()
        if not filtered or not data_idx:
            return
        page = max(ctrl._height - ctrl._head_end() - (len(filtered) - ctrl._tail_start()), 1)
        sel = ctrl.selected_choice_index
        if sel in data_idx:
            pos = data_idx.index(sel)
            new_pos = max(min(pos + direction * page, len(data_idx) - 1), 0)
        else:
            new_pos = 0
        ctrl.selected_choice_index = data_idx[new_pos]

    def _handle_home(self, event) -> None:
        data_idx = self._data_indices()
        if data_idx:
            self.content_control.selected_choice_index = data_idx[0]

    def _handle_end(self, event) -> None:
        data_idx = self._data_indices()
        if data_idx:
            self.content_control.selected_choice_index = data_idx[-1]

    def _handle_enter(self, event):
        try:
            selection = self.content_control.selection
        except IndexError:
            selection = None
        if selection is not None and self._is_non_selectable(selection):
            return
        super()._handle_enter(event)

    def _handle_toggle_all(self, event, value=None):
        if not self._multiselect:
            return
        ctrl = self.content_control
        for choice in ctrl._filtered_choices:
            raw = ctrl.choices[choice["index"]]
            if isinstance(raw["value"], (FuzzySeparator, DisabledChoice)):
                continue
            raw["enabled"] = value if value else not raw["enabled"]


_fuzzy_mod.InquirerPyFuzzyControl = PinnedFuzzyControl
question_mapping["fuzzy"] = DotFuzzy

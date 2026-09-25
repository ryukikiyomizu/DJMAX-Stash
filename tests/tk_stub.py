"""
A tiny virtual Tkinter, just good enough to run the real GUI code headlessly.

The point isn't to draw anything: it's to let the GUI's wiring be exercised in
CI (tree population, selection -> plan mapping, event handling, progress
rendering) on machines without a display or without Tk installed.

Any widget method the stub doesn't know about is recorded in ``widget.unknown``
so tests can assert the GUI never calls something that doesn't exist.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Callable, Dict, List, Optional


class TclError(Exception):
    pass


# ---------------------------------------------------------------------------
# variables
# ---------------------------------------------------------------------------


class Variable:
    def __init__(self, master=None, value=None, name=None):
        self._value = value
        self._traces: List[Callable] = []

    def get(self):
        return self._value

    def set(self, value):
        changed = value != self._value
        self._value = value
        if changed:
            for trace in list(self._traces):
                try:
                    trace()
                except Exception:
                    pass

    def trace_add(self, _mode, callback):
        self._traces.append(callback)
        return f"trace{len(self._traces)}"

    def trace_remove(self, *_a):
        pass

    # Tk's older API, in case anything uses it
    def trace(self, _mode, callback):
        return self.trace_add(_mode, callback)


class StringVar(Variable):
    def __init__(self, master=None, value="", name=None):
        super().__init__(master, value if value is not None else "")


class BooleanVar(Variable):
    def __init__(self, master=None, value=False, name=None):
        super().__init__(master, bool(value))


class IntVar(Variable):
    def __init__(self, master=None, value=0, name=None):
        super().__init__(master, int(value or 0))

    def get(self):
        try:
            return int(self._value)
        except (TypeError, ValueError):
            raise TclError(f"expected int, got {self._value!r}")


class DoubleVar(Variable):
    def __init__(self, master=None, value=0.0, name=None):
        super().__init__(master, float(value or 0.0))


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------


class Widget:
    """Base widget: geometry managers are no-ops, unknown calls are recorded."""

    _counter = 0

    def __init__(self, master=None, **kw):
        Widget._counter += 1
        self._id = f"w{Widget._counter}"
        self.master = master
        self.kw = dict(kw)
        self.unknown: List[str] = []
        self.bindings: Dict[str, Callable] = {}
        self.state = {"mapped": False}
        self.geometry_calls: List[tuple] = []
        if master is not None and hasattr(master, "_children"):
            master._children.append(self)

    # geometry
    def pack(self, **kw):
        self.state["mapped"] = True
        self.geometry_calls.append(("pack", dict(kw)))
        return self

    def pack_forget(self, **kw):
        self.state["mapped"] = False
        self.geometry_calls.append(("pack_forget", {}))

    def grid(self, **kw):
        self.state["mapped"] = True
        self.geometry_calls.append(("grid", dict(kw)))
        return self

    def grid_forget(self, **kw):
        self.state["mapped"] = False
        self.geometry_calls.append(("grid_forget", {}))

    def place(self, **kw):
        self.state["mapped"] = True
        self.geometry_calls.append(("place", dict(kw)))

    def columnconfigure(self, *a, **kw):
        weights = getattr(self, "_col_weights", {})
        if a:
            weights[int(a[0])] = kw.get("weight", weights.get(int(a[0]), 0))
        self._col_weights = weights

    def rowconfigure(self, *a, **kw):
        weights = getattr(self, "_row_weights", {})
        if a:
            weights[int(a[0])] = kw.get("weight", weights.get(int(a[0]), 0))
        self._row_weights = weights

    def rowweight(self, index):
        return getattr(self, "_row_weights", {}).get(index, 0)

    def colweight(self, index):
        return getattr(self, "_col_weights", {}).get(index, 0)

    def configure(self, *a, **kw):
        self.kw.update(kw)
        if a and isinstance(a[0], dict):
            self.kw.update(a[0])
        return self.kw

    config = configure  # alias, like the real Tk widgets

    def cget(self, key):
        return self.kw.get(key, "")

    def bind(self, sequence, func=None, add=None):
        self.bindings[sequence] = func
        return func

    def event_generate(self, sequence, **kw):
        func = self.bindings.get(sequence)
        if callable(func):
            func(types.SimpleNamespace(widget=self, **kw))

    def winfo_exists(self):
        return True

    def winfo_width(self):
        return 800

    def winfo_height(self):
        return 600

    def winfo_children(self):
        return list(getattr(self, "_children", []))

    def destroy(self):
        pass

    def __getattr__(self, name):
        # Catch-all: record and return a no-op so tests can flag unknown calls.
        if name.startswith("_"):
            raise AttributeError(name)

        def recorder(*args, **kwargs):
            self.unknown.append(name)
            return None

        return recorder


class Frame(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._children: List[Widget] = []


class Label(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.text = kw.get("text", "")

    def configure(self, *a, **kw):
        if "text" in kw:
            self.text = kw["text"]
        return super().configure(*a, **kw)

    config = configure


class Button(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.command = kw.get("command")
        self.text = kw.get("text", "")
        self.state_value = kw.get("state", "normal")

    def invoke(self):
        if self.command and self.state_value != "disabled":
            return self.command()

    def configure(self, *a, **kw):
        if "command" in kw:
            self.command = kw["command"]
        if "state" in kw:
            self.state_value = kw["state"]
        if "text" in kw:
            self.text = kw["text"]
        return super().configure(*a, **kw)

    config = configure


class Entry(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.textvariable = kw.get("textvariable")

    def get(self):
        return self.textvariable.get() if self.textvariable else ""

    def delete(self, *a):
        if self.textvariable:
            self.textvariable.set("")

    def insert(self, _index, value):
        if self.textvariable:
            self.textvariable.set(value)


class Checkbutton(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.variable = kw.get("variable")
        self.command = kw.get("command")
        self.text = kw.get("text", "")

    def invoke(self):
        if self.variable is not None:
            self.variable.set(not self.variable.get())
        if self.command:
            self.command()


class Spinbox(Entry):
    pass


class Combobox(Entry):
    """Enough of ttk.Combobox for the app: values, current(), set(), events."""

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.values = list(kw.get("values", []))
        self.state_value = kw.get("state", "normal")
        if self.textvariable is not None and not self.textvariable.get() and self.values:
            self.textvariable.set(self.values[0])

    def current(self, index=None):
        if index is None:
            value = self.textvariable.get() if self.textvariable else ""
            return self.values.index(value) if value in self.values else -1
        self.set(self.values[index])

    def set(self, value):
        if self.textvariable is not None:
            self.textvariable.set(value)

    def configure(self, *a, **kw):
        if "values" in kw:
            self.values = list(kw["values"])
        if "state" in kw:
            self.state_value = kw["state"]
        return super().configure(*a, **kw)

    config = configure


class Canvas(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.items: Dict[str, dict] = {}
        self._n = 0

    def create_oval(self, *a, **kw):
        self._n += 1
        key = f"oval{self._n}"
        self.items[key] = dict(kw)
        return key

    def create_rectangle(self, *a, **kw):
        self._n += 1
        key = f"rect{self._n}"
        self.items[key] = dict(kw)
        return key

    def itemconfig(self, key, **kw):
        self.items.setdefault(key, {}).update(kw)

    def delete(self, *a):
        self.items.clear()

    def pack(self, **kw):
        return super().pack(**kw)


class Text(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.content: List[str] = []
        self.tags: Dict[str, dict] = {}
        self._state = kw.get("state", "normal")

    def tag_configure(self, name, **kw):
        self.tags[name] = kw

    def insert(self, index, text, *tags):
        self.content.append(text)

    def delete(self, *a):
        self.content.clear()

    def index(self, spec):
        lines = "".join(self.content).count("\n") + 1
        return f"{lines}.0" if spec.startswith("end") else "1.0"

    def see(self, *a):
        pass

    def configure(self, *a, **kw):
        if "state" in kw:
            self._state = kw["state"]
        return super().configure(*a, **kw)

    config = configure

    def get_text(self) -> str:
        return "".join(self.content)


class Progressbar(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.maximum = float(kw.get("maximum", 100.0))
        self.value = float(kw.get("value", 0.0))

    def configure(self, *a, **kw):
        if "maximum" in kw:
            self.maximum = float(kw["maximum"])
        if "value" in kw:
            self.value = float(kw["value"])
        return super().configure(*a, **kw)

    config = configure


class Scrollbar(Widget):
    def set(self, *a):
        pass


class Separator(Widget):
    pass


# ---------------------------------------------------------------------------
# Treeview -- the one that actually matters
# ---------------------------------------------------------------------------


class Treeview(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._items: Dict[str, dict] = {}
        self._order: List[str] = []
        self._tree_children: Dict[str, List[str]] = {"": []}
        self._selected: List[str] = []
        self._focus = ""
        self._tags: Dict[str, dict] = {}
        self._columns: Dict[str, dict] = {}
        self._headings: Dict[str, str] = {}
        self._n = 0

    # -- model --------------------------------------------------------------
    def insert(self, parent="", index="end", iid=None, **kw):
        if iid is None:
            self._n += 1
            iid = f"I{self._n:04d}"
        parent = parent or ""
        self._tree_children.setdefault(parent, [])
        if index in ("end", "0", None) or str(index) == "end":
            self._tree_children[parent].append(iid)
        else:
            try:
                self._tree_children[parent].insert(int(index), iid)
            except (ValueError, TypeError):
                self._tree_children[parent].append(iid)
        self._order.append(iid)
        self._tree_children.setdefault(iid, [])
        self._items[iid] = {
            "text": kw.get("text", ""),
            "values": kw.get("values", ()),
            "tags": tuple(kw.get("tags", ()) or ()),
            "open": bool(kw.get("open", False)),
            "parent": parent,
        }
        return iid

    def get_children(self, item=""):
        return list(self._tree_children.get(item or "", []))

    def parent(self, item):
        return self._items.get(item, {}).get("parent", "")

    def delete(self, *items):
        for item in items:
            for child in self.get_children(item):
                self.delete(child)
            self._items.pop(item, None)
            self._tree_children.pop(item, None)
            if item in self._order:
                self._order.remove(item)
            parent = self._items.get(item, {}).get("parent")
            for kids in self._tree_children.values():
                if item in kids:
                    kids.remove(item)
            if item in self._selected:
                self._selected.remove(item)

    def item(self, item, option=None, **kw):
        if item not in self._items:
            return None if option else {}
        if kw:
            self._items[item].update({k: (tuple(v) if k == "tags" else v)
                                      for k, v in kw.items()})
        if option:
            return self._items[item].get(option)
        return dict(self._items[item])

    def exists(self, item):
        return item in self._items

    def move(self, item, parent, index="end"):
        for kids in self._tree_children.values():
            if item in kids:
                kids.remove(item)
        parent = parent or ""
        self._tree_children.setdefault(parent, [])
        if index in ("end", "0", None):
            self._tree_children[parent].append(item)
        else:
            self._tree_children[parent].insert(int(index), item)
        self._items[item]["parent"] = parent

    # Tk spells the same operation both ways
    reattach = move

    def detach(self, item):
        """Unmap an item but keep its parent, exactly like Tk does."""
        for kids in self._tree_children.values():
            if item in kids:
                kids.remove(item)

    # -- selection ----------------------------------------------------------
    def selection(self):
        return list(self._selected)

    def selection_set(self, items):
        self._selected = list(items)

    def selection_add(self, items):
        self._selected.extend(items)

    def selection_remove(self, items):
        for item in items:
            if item in self._selected:
                self._selected.remove(item)

    def focus(self, item=None):
        if item is not None:
            self._focus = item
            return None
        return self._focus

    def see(self, *a):
        pass

    def yview(self, *a):
        pass

    # -- misc ---------------------------------------------------------------
    def tag_configure(self, name, **kw):
        self._tags[name] = kw

    def column(self, name, **kw):
        self._columns.setdefault(name, {}).update(kw)

    def heading(self, name, **kw):
        self._headings[name] = kw.get("text", "")

    # helpers for tests
    def all_nodes(self) -> Dict[str, dict]:
        return {k: dict(v) for k, v in self._items.items()}

    def find(self, text_contains: str) -> Optional[str]:
        for iid, info in self._items.items():
            if text_contains.lower() in str(info["text"]).lower():
                return iid
        return None


class Notebook(Frame):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.tabs: List[Widget] = []

    def add(self, widget, **kw):
        self.tabs.append(widget)
        widget.kw.update(kw)
        widget.state["tab"] = True

    def select(self):
        return self.tabs[0] if self.tabs else None


# ---------------------------------------------------------------------------
# styled widgets
# ---------------------------------------------------------------------------


class Style:
    def __init__(self, master=None):
        self.settings: Dict[str, dict] = {}
        self.maps: Dict[str, dict] = {}

    def theme_use(self, name=None):
        return "clam"

    def theme_names(self):
        return ("clam",)

    def configure(self, name, **kw):
        self.settings.setdefault(name, {}).update(kw)

    def map(self, name, **kw):
        self.maps.setdefault(name, {}).update(kw)

    def lookup(self, name, option):
        return self.settings.get(name, {}).get(option)


# ---------------------------------------------------------------------------
# root, dialogs
# ---------------------------------------------------------------------------


class PhotoImage:
    def __init__(self, master=None, **kw):
        self.pixels: List[tuple] = []

    def put(self, colour, to=None):
        self.pixels.append((colour, to))


class _DialogRecorder:
    def __init__(self, kind):
        self.kind = kind
        self.calls: List[tuple] = []

    def __call__(self, title="", message="", **kw):
        self.calls.append((title, message))
        return "ok"


class Messagebox:
    def __init__(self):
        self.showerror = _DialogRecorder("error")
        self.showwarning = _DialogRecorder("warning")
        self.showinfo = _DialogRecorder("info")
        self.askyesno = _DialogRecorder("yesno")

    def reset(self):
        for recorder in (self.showerror, self.showwarning, self.showinfo, self.askyesno):
            recorder.calls.clear()

    @property
    def all_calls(self):
        out = []
        for recorder in (self.showerror, self.showwarning, self.showinfo, self.askyesno):
            out.extend((recorder.kind,) + call for call in recorder.calls)
        return out


class Filedialog:
    def __init__(self):
        self.directory = ""

    def askdirectory(self, **kw):
        return self.directory

    def askopenfilename(self, **kw):
        return ""

    def asksaveasfilename(self, **kw):
        return ""


class Tk(Widget):
    """Root window with a virtual clock so after() loops can be pumped."""

    def __init__(self):
        super().__init__(None)
        self._children: List[Widget] = []
        self._timers: List[dict] = []
        self.clock = 0.0          # milliseconds of virtual time
        self.title_text = ""
        self.geometry_text = ""
        self.mainloop_ran = False

    # -- window bits --------------------------------------------------------
    def title(self, text=None):
        if text is None:
            return self.title_text
        self.title_text = text

    def geometry(self, spec=None):
        if spec is None:
            return self.geometry_text
        self.geometry_text = spec

    def minsize(self, *a):
        if len(a) >= 2:
            self.minsize_width, self.minsize_height = int(a[0]), int(a[1])
        elif len(a) == 1 and isinstance(a[0], (tuple, list)):
            self.minsize_width, self.minsize_height = int(a[0][0]), int(a[0][1])
        return (getattr(self, "minsize_width", 0), getattr(self, "minsize_height", 0))

    def resizable(self, *a):
        pass

    def iconphoto(self, *a):
        pass

    def configure(self, *a, **kw):
        return super().configure(*a, **kw)

    config = configure

    def call(self, *a, **kw):
        return ""

    def option_add(self, pattern=None, value=None, priority=None):
        return None

    def destroy(self):
        pass

    def mainloop(self, n=0):
        self.mainloop_ran = True
        self.pump_until(lambda: False, seconds=0.05)

    def quit(self):
        pass

    # -- timers -------------------------------------------------------------
    def after(self, ms, func=None, *args):
        if func is None:
            self.clock += float(ms)
            return f"t{len(self._timers)}"
        handle = {"due": self.clock + float(ms), "func": func, "args": args,
                  "id": f"t{len(self._timers)}", "cancelled": False}
        self._timers.append(handle)
        return handle["id"]

    def after_cancel(self, handle):
        for timer in self._timers:
            if timer["id"] == handle:
                timer["cancelled"] = True

    def after_idle(self, func, *args):
        return self.after(0, func, *args)

    def update(self):
        self._run_due()

    def update_idletasks(self):
        self._run_due()

    # -- pump ---------------------------------------------------------------
    def _run_due(self) -> int:
        ran = 0
        for timer in sorted(self._timers, key=lambda t: t["due"]):
            if timer["due"] <= self.clock and not timer["cancelled"]:
                timer["cancelled"] = True
                try:
                    timer["func"](*timer["args"])
                except Exception as exc:  # surface GUI errors in tests
                    self.errors.append(exc)
                ran += 1
        self._timers = [t for t in self._timers if not t["cancelled"]]
        return ran

    errors: List[Exception] = []

    def step(self, advance_ms: float = 100.0, sleep: float = 0.005):
        """Advance virtual time, run due callbacks, and let worker threads breathe."""
        self.clock += advance_ms
        self._run_due()
        if sleep:
            time.sleep(sleep)

    def pump_until(self, predicate, seconds: float = 30.0, advance_ms: float = 100.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.step(advance_ms)
            if predicate():
                return True
        return bool(predicate())


# ---------------------------------------------------------------------------
# module factory
# ---------------------------------------------------------------------------


def install(dialog: Optional[Messagebox] = None, filedialog: Optional[Filedialog] = None):
    """Install the stub as ``tkinter`` (and ttk/filedialog/messagebox)."""
    dialog = dialog or Messagebox()
    filedialog = filedialog or Filedialog()

    tkinter = types.ModuleType("tkinter")
    for name, value in list(globals().items()):
        if name in ("TclError", "Variable", "StringVar", "BooleanVar", "IntVar", "DoubleVar",
                    "Widget", "Frame", "Label", "Button", "Entry", "Checkbutton", "Spinbox",
                    "Canvas", "Text", "Progressbar", "Scrollbar", "Separator", "Treeview",
                    "Style", "PhotoImage", "Tk"):
            setattr(tkinter, name, value)
    tkinter.__version__ = "8.6-stub"

    ttk = types.ModuleType("tkinter.ttk")
    for name, value in list(globals().items()):
        if name in ("Style", "Frame", "Label", "Button", "Entry", "Checkbutton", "Spinbox",
                    "Combobox", "Notebook", "Progressbar", "Scrollbar", "Separator",
                    "Treeview"):
            setattr(ttk, name, value)
    ttk.Widget = Widget  # tkinter.ttk re-exports Widget

    filedialog_module = types.ModuleType("tkinter.filedialog")
    for name in ("askdirectory", "askopenfilename", "asksaveasfilename"):
        setattr(filedialog_module, name, getattr(filedialog, name))

    messagebox_module = types.ModuleType("tkinter.messagebox")
    for name in ("showerror", "showwarning", "showinfo", "askyesno"):
        setattr(messagebox_module, name, getattr(dialog, name))

    tkinter.ttk = ttk
    tkinter.messagebox = messagebox_module
    tkinter.filedialog = filedialog_module

    sys.modules["tkinter"] = tkinter
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = messagebox_module
    sys.modules["tkinter.filedialog"] = filedialog_module
    return tkinter, dialog, filedialog

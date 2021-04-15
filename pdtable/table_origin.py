"""
The purpose of the `table_origin` module is to provide an object model of the origin of a given table

The `table_origin` module defines the data structure for achieving this. The process of building the object
model is performed by the load system. The object model is attached to `Table`-objects as a `TableOrigin` instance
in the `.origin` field of `.metadata`.
"""


from typing import (
    DefaultDict,
    Protocol,
    Iterator,
    Any,
    NamedTuple,
    Iterable,
    Optional,
    Set,
    Tuple,
    Callable,
    List,
    Dict,
    Union,
)
from abc import abstractmethod, abstractproperty, abstractstaticmethod, ABC
import logging, time
from dataclasses import dataclass, field
from pathlib import Path, PosixPath
import sys, os, subprocess, datetime, html, random, base64


class LoadLocation(Protocol):
    @property
    @abstractmethod
    def local_folder_path(self) -> Optional[Path]:
        """
        Used for resolving relative imports
        """
        pass

    @property
    @abstractmethod
    def load_specification(self) -> "LoadItem":
        pass

    @property
    @abstractmethod
    def load_identifier(self) -> str:
        pass

    @property
    def interactive_identifier(self) -> str:
        pass

    @abstractmethod
    def interactive_open(self, read_only: bool = False):
        pass

    @abstractmethod
    def interactive_uri(self, read_only: bool = False) -> str:
        pass


class LoadItem(NamedTuple):
    specification: str
    source: Optional[LoadLocation]

    @property
    def source_identifier(self) -> str:
        return "<root>" if self.source is None else self.source.load_identifier

    def load_history(self) -> Iterator["LoadItem"]:
        """
        Return the load tree leading up to this load item

        Typical use:
        ```
        '\n'.join(f'included as "{li.spec} from "{li.source_identifier}"' for li in load_specification.load_history())
        ```

        Example text representation
        ```
        included as "doreco:input_foo_DOR12345" from "/mp/input_include.csv@2021-01-02T1233" row 27
        included as "input_include.csv" from "/mp"
        included as "mp/" from "/input_all.csv@123123123" row 12
        included as "input_all.csv" from "/"
        included as "/" from "<root>"
        ```
        """
        yield self
        if self.source:
            yield from self.source.load_specification.load_history()

    def __str__(self) -> str:
        return ";".join(
            f'included as "{li.specification}" from "{li.source.interactive_identifier if li.source else "<root>"}"'
            for li in self.load_history()
        )


def interactive_open_uri(uri):
    """
    A cross-platform functionality for launching tool by URI
    """
    if sys.platform == "win32":
        os.startfile(uri)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.call([opener, uri])


class LocationFile(Protocol):
    """
    Represents a traceable load entity

    The load entity could be a file, a blob, an http response. The LocationFile instance
    should hold enough information to uniquely identify (and if possible allow recreation)
    of the block stream resulting of loading this entity.
    """

    @property
    @abstractmethod
    def load_specification(self) -> LoadItem:
        """
        The specification passed to Loader instance as LoadItem to retrieve this file

        The specification may not uniquely identify the file, as i may include partial
        record specifications (e.g. "use latest") resolved at load time.
        """
        pass

    @property
    @abstractmethod
    def load_identifier(self) -> str:
        """
        Unique identifier of loaded item
        
        Must be unique to allow import loop detection and input caching.
        For local files, this could be absolute path and modification time.
        """
        pass

    @property
    @abstractmethod
    def local_path(self) -> Optional[Path]:
        pass

    @property
    def local_folder_path(self) -> Optional[Path]:
        return None if self.local_path is None else self.local_path.parent

    @abstractmethod
    def interactive_uri(
        self, sheet: Optional[str] = None, row: Optional[int] = None, read_only=True
    ) -> Optional[str]:
        pass

    def interactive_open(
        self, sheet: Optional[str] = None, row: Optional[int] = None, read_only=True
    ):
        uri = self.interactive_uri(sheet, row, read_only)
        interactive_open_uri(uri)

    def get_interactive_identifier(
        self, sheet: Optional[str] = None, row: Optional[int] = None
    ) -> str:
        """
        Defaults to load identifier, but may be replaced with replacement better suited for interactive use
        """
        s_loc = "" if sheet is None else f" Sheet '{sheet}'"
        r_loc = "" if row is None else f" Row {row}"

        return self.load_identifier + s_loc + r_loc

    @property
    def interactive_identifier(self) -> str:
        return self.get_interactive_identifier()

    def get_local_path(self) -> Path:
        """
        Always return path to local instance

        Will write contents to local storage if not available.
        """
        if self.local_path is not None:
            return self.local_path
        raise NotImplementedError("Automatic download not implemented")

    def make_location_sheet(self, sheet_name: Optional[str] = None):
        return LocationSheet(file=self, sheet_name=sheet_name)

    def __str__(self) -> str:
        return self.interactive_identifier

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(id={repr(self.load_identifier)}, "
            f"spec={repr(self.load_specification)})"
        )


def _random_id() -> str:
    return base64.b32encode(os.urandom(5 * 2)).decode("ascii")


class NullLocationFile(LocationFile):
    """
    Null-implementation of LocationFile
    """

    def __init__(self, description: Optional[str] = None, id: Optional[str] = None):
        if description is None:
            description = "Unknown"
        if id is None:
            id = f"{description}-{_random_id()}"
        self._spec = LoadItem(description, source=None)
        self._load_identifier = id

    @property
    def load_specification(self) -> LoadItem:
        return self._spec

    @property
    def load_identifier(self) -> str:
        return self._load_identifier

    @property
    def local_path(self) -> Optional[Path]:
        return None

    def interactive_uri(
        self, sheet: Optional[str] = None, row: Optional[int] = None, read_only=True
    ) -> Optional[str]:
        return None


class FilesystemLocationFile(LocationFile):
    """
    Args:
        root_folder: If specified, interactive_identifier will be given relative to this
    """

    def __init__(
        self,
        local_path: Path,
        load_specification: Optional[LoadItem] = None,
        root_folder: Optional[Path] = None,
        stat_result=None,
    ) -> None:
        self._local_path = local_path
        self._load_specification = load_specification or LoadItem(
            specification=str(local_path), source=None
        )
        self._stat_result = stat_result
        self.root_folder = root_folder

    @property
    def local_path(self):
        return self._local_path

    @property
    def load_specification(self) -> LoadItem:
        return self._load_specification

    def get_stat_result(self, cached=True):
        if (not cached) or self._stat_result is None:
            self._stat_result = self.local_path.stat()
        return self._stat_result

    def get_mtime(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.get_stat_result().st_mtime)

    @property
    def load_identifier(self) -> str:
        name_part = str(self.local_path.absolute())
        mtime = self.get_mtime()
        if mtime:
            return name_part + "@" + mtime.isoformat(timespec="seconds")
        return name_part

    @property
    def interactive_identifier(self) -> str:
        if self.root_folder is None:
            return str(self.local_path)
        return str(self.local_path.relative_to(self.root_folder))

    def get_interactive_identifier(
        self, sheet: Optional[str] = None, row: Optional[int] = None
    ) -> str:
        if sheet is None:
            loc = f"Row {row}"
        else:
            loc = f"'{sheet}'!A{row}"
        return f"{loc} of '{self.interactive_identifier}'"

    def interactive_uri(
        self,
        sheet: Optional[str] = None,
        row: Optional[int] = None,
        read_only: Optional[bool] = False,
    ) -> str:
        file_uri = self.local_path.as_uri()
        if sheet is None and row is None:
            return file_uri
        if sheet is None:
            sheet = "Sheet1"
        row_mark = f"!A{row}" if row is not None else ""
        return file_uri + f"#'{sheet}'{row_mark}"


class LocationFolder(NamedTuple):
    local_folder_path: Path
    load_specification: LoadItem
    root_folder: Optional[Path] = None

    @property
    def load_identifier(self) -> str:
        return str(self.local_folder_path)

    @property
    def interactive_identifier(self) -> str:
        if self.root_folder is None:
            return self.load_identifier
        rel_path = self.local_folder_path.relative_to(self.root_folder)
        if rel_path == Path("."):
            return f"<root_folder: {self.root_folder}>"
        else:
            return str(rel_path)

    def interactive_uri(self, read_only=False) -> str:
        return self.local_folder_path.as_uri()

    def interactive_open(self):
        interactive_open_uri(self.interactive_uri())

    @classmethod
    def make_location_folder(
        cls,
        local_folder_path: Path,
        load_specification: LoadItem = None,
        root_folder: Optional[Path] = None,
    ) -> "LocationFolder":
        if load_specification is None:
            load_specification = LoadItem(str(local_folder_path), source=None)
        return cls(local_folder_path=local_folder_path, load_specification=load_specification, root_folder=root_folder)


@dataclass(frozen=True)  # to allow empty dict default
class LocationSheet:
    file: LocationFile
    sheet_name: Optional[str]
    sheet_metadata: Dict[str, str] = field(default_factory=dict)

    def make_location_block(self, row: int):
        return LocationBlock(sheet=self, row=row)


class LocationBlock(NamedTuple):
    """

    """

    sheet: LocationSheet
    row: int

    @property
    def file(self) -> LocationFile:
        return self.sheet.file

    @property
    def sheet_name(self) -> Optional[str]:
        return self.sheet.sheet_name

    @property
    def local_folder_path(self) -> Optional[Path]:
        return self.file.local_folder_path

    @property
    def load_identifier(self) -> str:
        return f"{self.file.load_identifier}#'{self.sheet_name or 'Sheet1'}'!A{self.row}"

    @property
    def load_specification(self) -> LoadItem:
        return self.file.load_specification

    @property
    def interactive_identifier(self) -> str:
        return self.file.get_interactive_identifier(sheet=self.sheet_name, row=self.row)

    def interactive_uri(self, read_only: bool = False):
        return self.file.interactive_uri(sheet=self.sheet_name, row=self.row, read_only=read_only)

    def interactive_open(self, read_only: bool = False):
        interactive_open_uri(self.interactive_uri(read_only=read_only))

    def __str__(self) -> str:
        return f"{self.interactive_identifier};{self.file.load_specification}"

    def __repr__(self) -> str:
        sheet_spec = "" if self.sheet_name is None else f", sheet={self.sheet_name}"
        return f"LocationBlock(row={self.row}{sheet_spec}, file={repr(self.file)})"


@dataclass(frozen=True)
class TableOrigin:
    """
    A TableOrigin instance defines the source of a Table instance.

    The source may either be a loaded input table or an operation combining multiple
    parents (represented by TableOrigin instances) into a derived table, so that each
    ``TableOrigin``-instance is the root of a tree of ``TableOrigin`` instances.

    Each node in the tree is either a _leaf_, corresponding to a loaded input, in which 
    case only ``input_location`` is defined, or a _branch_, corresponding to a derived
    table, in which case only ``parents`` and ``operation`` are defined.
    
    For an integrated representation of this information, an application should traverse
    the tree and provide a representation in the most convenient form. For an example, 
    see ``table_origin_as_html`` or ``table_origin_as_str``.
    """

    input_location: Optional[LocationBlock] = None
    parents: Iterable["TableOrigin"] = ()
    operation: Optional[str] = None

    def _post_init_(self):
        if self.operation is None:
            # leaf
            if self.parents or self.input_location is None:
                raise ValueError("For TableOrigin leaf node, only input_location must be defined")
        else:
            # branch
            if self.input_location is not None or not self.parents:
                raise ValueError(
                    "For TableOrigin branch node, only operation and parents should be defined"
                )

    @property
    def is_leaf(self):
        return self.operation is None

    def get_input_ancestors(self) -> Iterator[LocationBlock]:
        """
        Return iterator over the input-location of all non-derived ancestors
        """
        if self.is_leaf:
            if self.input_location:
                yield self.input_location
            else:
                raise ValueError("Inconsistent state of TableOrigin")
        else:
            for p in self.parents:
                yield from p.get_input_ancestors()

    def __str__(self):
        return table_origin_as_str(self)

    def _repr_html_(self) -> str:
        return table_origin_as_html(self)


def table_origin_as_html(tt: TableOrigin):
    def visit(tt):
        return visit_branch(tt) if tt.input_location is None else visit_leaf(tt)

    def visit_leaf(tt):
        loc = tt.input_location
        return (
            f"""<a href="{loc.interactive_uri()}" class="input-table-origin">"""
            f"""{html.escape(loc.interactive_identifier)}</a>"""
        )

    def visit_branch(tt):
        return (
            f"""<div class="derived-table-origin"><span>{html.escape(tt.operation)}</span><ul>"""
            + "\n".join(f"<li>{visit(p)}</li>" for p in tt.parents)
            + """</ul></div>"""
        )

    return visit(tt)


def table_origin_as_str(tt: TableOrigin):
    buf: List[Tuple[int, str]] = []

    def visit(tt, lev):
        return visit_branch(tt, lev) if tt.input_location is None else visit_leaf(tt, lev)

    def visit_leaf(tt, lev):
        buf.append((lev, tt.input_location.interactive_identifier))

    def visit_branch(tt, lev):
        buf.append((lev, f"Derived via {tt.operation} from:"))
        for p in tt.parents:
            visit(p, lev + 1)

    visit(tt, 0)
    return "\n".join("  " * lev + s for lev, s in buf)


@dataclass
class InputIssue:
    issue: Union[str, Exception]
    load_item: Optional[LoadItem] = None
    location_file: Optional[LocationFile] = None
    origin: Optional[TableOrigin] = None
    severity: int = logging.ERROR


class InputIssueTracker(ABC):
    """
    Protocol for tracking issues across inputs
    """

    @abstractmethod
    def add_issue(self, input_issue: InputIssue):
        pass

    def add_error(
        self,
        issue: Union[str, Exception],
        load_item: Optional[LoadItem] = None,
        location_file: Optional[LocationFile] = None,
        origin: Optional[TableOrigin] = None,
    ):
        self.add_issue(
            InputIssue(
                load_item=load_item,
                location_file=location_file,
                origin=origin,
                issue=issue,
                severity=logging.ERROR,
            )
        )

    def add_warning(
        self,
        issue: Union[str, Exception],
        load_item: Optional[LoadItem] = None,
        location_file: Optional[LocationFile] = None,
        origin: Optional[TableOrigin] = None,
    ):
        """
        Add a warning about a non-critical input issue

        Examples include
        - additional columns compared to template
        - additional tables compared to template
        """
        self.add_issue(
            InputIssue(
                load_item=load_item,
                location_file=location_file,
                origin=origin,
                issue=issue,
                severity=logging.WARNING,
            )
        )

    @property
    @abstractmethod
    def issues(self) -> Iterable[InputIssue]:
        pass

    @property
    def is_ok(self) -> bool:
        """
        True if no errors have been registered
        """
        return not any(m.severity >= logging.ERROR for m in self.issues)


class InputError(Exception):
    """
    An exception raised on irrecoverable error in the input processing

    This exception should now be caught within the pdtable framework.
    """

    pass


class NullInputIssueTracker(InputIssueTracker):
    """
    Raise an exception immediately on first input issue
    """

    def add_issue(self, issue):
        raise InputError(issue)

    @property
    def is_ok(self):
        return True

    @property
    def issues(self):
        return ()


class WrappedInputIssueError(Exception):
    """
    An `InputIssue` instance wrapped in an exception to bubble up to layer
    with access to an InputIssueTracker

    This exception should always be caught somewhere in the pdtable stack
    and added to the issue tracker. For errors where this does not make sense, 
    use a different exception class.
    """

    pass


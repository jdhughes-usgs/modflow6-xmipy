import logging
import os
import platform
from ctypes import (
    CDLL,
    POINTER,
    byref,
    c_char,
    c_char_p,
    c_double,
    c_int,
    c_void_p,
    create_string_buffer,
)
from enum import Enum, IntEnum, unique
from pathlib import Path
from typing import Any, Callable, Tuple, Union

import numpy as np
from numpy.typing import NDArray

from xmipy.errors import InputError, TimerError, XMIError
from xmipy.logger import get_logger, show_logger_message
from xmipy.timers.timer import Timer
from xmipy.utils import cd, repr_function_call
from xmipy.xmi import Xmi


@unique
class Status(IntEnum):
    SUCCESS = 0
    FAILURE = 1


@unique
class State(Enum):
    UNINITIALIZED = 1
    INITIALIZED = 2


class XmiWrapper(Xmi):
    """The implementation of the XMI"""

    def __init__(
        self,
        lib_path: Union[str, Path],
        lib_dependency: Union[str, Path, None] = None,
        working_directory: Union[str, Path, None] = None,
        timing: bool = False,
        logger_level: Union[str, int] = 0,
    ):
        """
        Constructor of `XmiWrapper`

        Next to wrapping C functions of a library exposing XMI,
        it also adds timing and logging functionality.
        An example for logging can be seen below.

        ```
        In [1]: from xmipy import XmiWrapper

        In [2]: mf6 = XmiWrapper("/path/to/libmf6.so", working_directory="/path/to/sim", logger_level="DEBUG")

        In [3]: mf6.initialize()
        DEBUG:libmf6.so: execute function: initialize(b'') returned 0

        In [4]: mf6.get_start_time()
        DEBUG:libmf6.so: execute function: get_start_time(&c_double(0.0)) returned 0
        Out[4]: 0.0

        In [5]: mf6.get_end_time()
        DEBUG:libmf6.so: execute function: get_end_time(&c_double(504.0)) returned 0
        Out[5]: 504.0

        In [6]: mf6.get_grid_rank(1)
        DEBUG:libmf6.so: execute function: get_grid_rank(&c_int(1), &c_int(2)) returned 0
        Out[6]: 2

        In [7]: mf6.get_value('SLN_1/MXITER')
        DEBUG:libmf6.so: execute function: get_var_rank(c_char_p(b'SLN_1/MXITER'), &c_int(0)) returned 0
        DEBUG:libmf6.so: execute function: get_var_type(c_char_p(b'SLN_1/MXITER'), &c_char_Array_51(b'INTEGER')) returned 0
        DEBUG:libmf6.so: execute function: get_var_type(c_char_p(b'SLN_1/MXITER'), &c_char_Array_51(b'INTEGER')) returned 0
        DEBUG:libmf6.so: execute function: get_value_ptr_int(c_char_p(b'SLN_1/MXITER'), &ndpointer_<i4_1d_1_C) returned 0
        DEBUG:libmf6.so: execute function: get_var_type(c_char_p(b'SLN_1/MXITER'), &c_char_Array_51(b'INTEGER')) returned 0
        DEBUG:libmf6.so: execute function: get_value_ptr_int(c_char_p(b'SLN_1/MXITER'), &ndpointer_<i4_1d_1_C) returned 0
        Out[7]: array([25], dtype=int32)
        ```

        Parameters
        ----------
        lib_path : Union[str, Path]
            Path to the shared library

        lib_dependency : Union[str, Path, None], optional
            Path to the dependencies of the shared library, by default None

        working_directory : Union[str, Path, None], optional
            The working directory the shared library expects when being called,
            by default None

        timing : bool, optional
            Whether timing should be activated, by default False

        logger_level : str, int, optional
            Logger level, default 0 ("NOTSET"). Accepted values are
            "DEBUG" (10), "INFO" (20), "WARNING" (30), "ERROR" (40) or
            "CRITICAL" (50).
        """

        self._state = State.UNINITIALIZED
        self.libname = Path(lib_path).name
        self.logger = get_logger(self.libname, logger_level)

        if lib_dependency:
            self._add_lib_dependency(lib_dependency)
        # LoadLibraryEx flag (py38+): LOAD_WITH_ALTERED_SEARCH_PATH 0x08
        # -> uses the altered search path for resolving dll dependencies
        # `winmode` has no effect while running on Linux or macOS
        # Note: this could make xmipy less secure (dll-injection)
        # Can we get it to work without this flag?
        self.lib = CDLL(str(lib_path), winmode=0x08)

        if working_directory:
            self.working_directory = Path(working_directory)
        else:
            self.working_directory = Path().cwd()
        self.timing = timing

        if self.timing:
            self.timer = Timer(
                name=self.libname,
                text="Elapsed time for {name}.{fn_name}: {seconds:0.4f} seconds",
            )

    def __del__(self) -> None:
        if self._state == State.INITIALIZED:
            self.finalize()

    @staticmethod
    def _add_lib_dependency(lib_dependency: Union[str, Path]) -> None:
        lib_dependency = str(Path(lib_dependency).absolute())
        if platform.system() == "Windows":
            os.environ["PATH"] = lib_dependency + os.pathsep + os.environ["PATH"]
        else:
            # Assume a Unix-like system
            if "LD_LIBRARY_PATH" in os.environ:
                os.environ["LD_LIBRARY_PATH"] = (
                    lib_dependency + os.pathsep + os.environ["LD_LIBRARY_PATH"]
                )
            else:
                os.environ["LD_LIBRARY_PATH"] = lib_dependency

    def report_timing_totals(self) -> float:
        if self.timing:
            total = self.timer.report_totals()
            with show_logger_message(self.logger):
                self.logger.info(
                    "Total elapsed time for %s: %0.4f seconds",
                    self.libname,
                    total,
                )
            return total
        else:
            raise TimerError("Timing not activated")

    def get_constant_int(self, name: str) -> int:
        c_var = c_int.in_dll(self.lib, name)
        return c_var.value

    def set_int(self, name: str, value: int) -> None:
        c_var = c_int.in_dll(self.lib, name)
        c_var.value = value

    def initialize(self, config_file: str = "") -> None:
        if self._state == State.UNINITIALIZED:
            with cd(self.working_directory):
                self._execute_function(self.lib.initialize, config_file.encode())
                self._state = State.INITIALIZED
        else:
            raise InputError("The library is already initialized")

    def initialize_mpi(self, value: int) -> None:
        if self._state == State.UNINITIALIZED:
            with cd(self.working_directory):
                comm = c_int(value)
                self._execute_function(self.lib.initialize_mpi, byref(comm))
                self._state = State.INITIALIZED
        else:
            raise InputError("The library is already initialized")

    def update(self) -> None:
        with cd(self.working_directory):
            self._execute_function(self.lib.update)

    def update_until(self, time: float) -> None:
        with cd(self.working_directory):
            self._execute_function(self.lib.update_until, c_double(time))

    def finalize(self) -> None:
        if self._state == State.INITIALIZED:
            with cd(self.working_directory):
                self._execute_function(self.lib.finalize)
                self._state = State.UNINITIALIZED
        else:
            raise InputError("The library is not initialized yet")

    def get_current_time(self) -> float:
        current_time = c_double(0.0)
        self._execute_function(self.lib.get_current_time, byref(current_time))
        return current_time.value

    def get_start_time(self) -> float:
        start_time = c_double(0.0)
        self._execute_function(self.lib.get_start_time, byref(start_time))
        return start_time.value

    def get_end_time(self) -> float:
        end_time = c_double(0.0)
        self._execute_function(self.lib.get_end_time, byref(end_time))
        return end_time.value

    def get_time_step(self) -> float:
        dt = c_double(0.0)
        self._execute_function(self.lib.get_time_step, byref(dt))
        return dt.value

    def get_component_name(self) -> str:
        len_name = self.get_constant_int("BMI_LENCOMPONENTNAME")
        component_name = create_string_buffer(len_name)
        self._execute_function(self.lib.get_component_name, byref(component_name))
        return component_name.value.decode("ascii")

    def get_version(self) -> str:
        len_version = self.get_constant_int("BMI_LENVERSION")
        version = create_string_buffer(len_version)
        self._execute_function(self.lib.get_version, byref(version))
        return version.value.decode("ascii")

    def get_input_item_count(self) -> int:
        count = c_int(0)
        self._execute_function(self.lib.get_input_item_count, byref(count))
        return count.value

    def get_output_item_count(self) -> int:
        count = c_int(0)
        self._execute_function(self.lib.get_output_item_count, byref(count))
        return count.value

    def get_input_var_names(self) -> Tuple[str]:
        len_address = self.get_constant_int("BMI_LENVARADDRESS")
        nr_input_vars = self.get_input_item_count()
        len_names = nr_input_vars * len_address
        names = create_string_buffer(len_names)

        # get a (1-dim) char array (char*) containing the input variable
        # names as \x00 terminated sub-strings
        self._execute_function(self.lib.get_input_var_names, byref(names))

        # decode
        input_vars: Tuple[str] = tuple(
            names[i * len_address : (i + 1) * len_address]  # type: ignore
            .split(b"\0", 1)[0]
            .decode("ascii")
            for i in range(nr_input_vars)
        )
        return input_vars

    def get_output_var_names(self) -> Tuple[str]:
        len_address = self.get_constant_int("BMI_LENVARADDRESS")
        nr_output_vars = self.get_output_item_count()
        len_names = nr_output_vars * len_address
        names = create_string_buffer(len_names)

        # get a (1-dim) char array (char*) containing the output variable
        # names as \x00 terminated sub-strings
        self._execute_function(self.lib.get_output_var_names, byref(names))

        # decode
        output_vars: Tuple[str] = tuple(
            names[i * len_address : (i + 1) * len_address]  # type: ignore
            .split(b"\0", 1)[0]
            .decode("ascii")
            for i in range(nr_output_vars)
        )
        return output_vars

    def get_var_grid(self, name: str) -> int:
        grid_id = c_int(0)
        self._execute_function(
            self.lib.get_var_grid,
            c_char_p(name.encode()),
            byref(grid_id),
        )
        return grid_id.value

    def get_var_type(self, name: str) -> str:
        len_var_type = self.get_constant_int("BMI_LENVARTYPE")
        var_type = create_string_buffer(len_var_type)
        self._execute_function(
            self.lib.get_var_type,
            c_char_p(name.encode()),
            byref(var_type),
        )
        return var_type.value.decode()

    # strictly speaking not BMI...
    def get_var_shape(self, name: str) -> NDArray[np.int32]:
        rank = self.get_var_rank(name)
        array = np.zeros(rank, dtype=np.int32)
        self._execute_function(
            self.lib.get_var_shape,
            c_char_p(name.encode()),
            c_void_p(array.ctypes.data),
        )
        return array

    def get_var_rank(self, name: str) -> int:
        rank = c_int(0)
        self._execute_function(
            self.lib.get_var_rank,
            c_char_p(name.encode()),
            byref(rank),
        )
        return rank.value

    def get_var_units(self, name: str) -> str:
        raise NotImplementedError

    def get_var_itemsize(self, name: str) -> int:
        item_size = c_int(0)
        self._execute_function(
            self.lib.get_var_itemsize,
            c_char_p(name.encode()),
            byref(item_size),
        )
        return item_size.value

    def get_var_nbytes(self, name: str) -> int:
        nbytes = c_int(0)
        self._execute_function(
            self.lib.get_var_nbytes,
            c_char_p(name.encode()),
            byref(nbytes),
        )
        return nbytes.value

    def get_var_location(self, name: str) -> str:
        raise NotImplementedError

    def get_time_units(self) -> str:
        raise NotImplementedError

    def get_value(
        self, name: str, dest: Union[NDArray[Any], None] = None
    ) -> NDArray[Any]:
        # make sure that optional array is of correct layout:
        if dest is not None and not dest.flags["C"]:
            raise InputError("Array should have C layout")

        # first deal with scalars
        rank = self.get_var_rank(name)
        var_type = self.get_var_type(name)
        var_type_lower = var_type.lower()

        if rank == 0:
            if var_type_lower.startswith("string"):
                ilen = self.get_var_nbytes(name)
                strtype = "<S" + str(ilen + 1)
                if dest is None:
                    dest = np.empty(1, dtype=strtype, order="C")
                self._execute_function(
                    self.lib.get_value,
                    c_char_p(name.encode()),
                    byref(dest.ctypes.data_as(POINTER(c_char))),
                )
                dest[0] = dest[0].decode("ascii").strip()
                return dest.astype(str)
            else:
                src = self.get_value_ptr_scalar(name)
                if dest is None:
                    return self.get_value_ptr_scalar(name).copy()
                else:
                    dest[0] = src[0]
                    return dest

        var_shape = self.get_var_shape(name)

        if var_type_lower.startswith("double"):
            if dest is None:
                dest = np.empty(shape=var_shape, dtype=np.float64, order="C")
            self._execute_function(
                self.lib.get_value,
                c_char_p(name.encode()),
                byref(dest.ctypes.data_as(POINTER(c_double))),
            )
        elif var_type_lower.startswith("int"):
            if dest is None:
                dest = np.empty(shape=var_shape, dtype=np.int32, order="C")
            self._execute_function(
                self.lib.get_value,
                c_char_p(name.encode()),
                byref(dest.ctypes.data_as(POINTER(c_int))),
            )
        elif var_type_lower.startswith("string"):
            if dest is None:
                if var_shape[0] == 0:
                    return np.empty((0,), "U1")
                ilen = self.get_var_nbytes(name) // var_shape[0]
                strtype = "<S" + str(ilen + 1)
                dest = np.empty(var_shape[0], dtype=strtype, order="C")
            self._execute_function(
                self.lib.get_value,
                c_char_p(name.encode()),
                byref(dest.ctypes.data_as(POINTER(c_char))),
            )
            for i, x in enumerate(dest):
                dest[i] = x.decode("ascii").strip()
            return dest.astype(str)
        else:
            raise InputError(f"Unsupported value type {var_type!r}")

        return dest

    def get_value_ptr(self, name: str) -> NDArray[Any]:
        # first scalars
        rank = self.get_var_rank(name)
        if rank == 0:
            return self.get_value_ptr_scalar(name)

        var_type = self.get_var_type(name)
        var_type_lower = var_type.lower()
        shape_array = self.get_var_shape(name)

        # convert shape array to python tuple
        shape_tuple = tuple(np.trim_zeros(shape_array))
        ndim = len(shape_tuple)

        if var_type_lower.startswith("double"):
            arraytype = np.ctypeslib.ndpointer(
                dtype=np.float64, ndim=ndim, shape=shape_tuple, flags="C"
            )
        elif var_type_lower.startswith("float"):
            arraytype = np.ctypeslib.ndpointer(
                dtype=np.float32, ndim=ndim, shape=shape_tuple, flags="C"
            )
        elif var_type_lower.startswith("int"):
            arraytype = np.ctypeslib.ndpointer(
                dtype=np.int32, ndim=ndim, shape=shape_tuple, flags="C"
            )
        else:
            raise InputError(f"Unsupported value type {var_type!r}")
        values = arraytype()
        self._execute_function(
            self.lib.get_value_ptr,
            c_char_p(name.encode()),
            byref(values),
            detail="for variable " + name,
        )
        return values.contents

    def get_value_ptr_scalar(self, name: str) -> NDArray[Any]:
        var_type = self.get_var_type(name)
        var_type_lower = var_type.lower()
        if var_type_lower.startswith("double"):
            arraytype = np.ctypeslib.ndpointer(
                dtype=np.float64, ndim=1, shape=(1,), flags="C"
            )
        elif var_type_lower.startswith("float"):
            arraytype = np.ctypeslib.ndpointer(
                dtype=np.float32, ndim=1, shape=(1,), flags="C"
            )
        elif var_type_lower.startswith("int"):
            arraytype = np.ctypeslib.ndpointer(
                dtype=np.int32, ndim=1, shape=(1,), flags="C"
            )
        else:
            raise InputError(f"Unsupported value type {var_type!r}")
        values = arraytype()
        self._execute_function(
            self.lib.get_value_ptr,
            c_char_p(name.encode()),
            byref(values),
            detail="for variable " + name,
        )
        return values.contents

    def get_value_at_indices(
        self, name: str, dest: NDArray[Any], inds: NDArray[np.int32]
    ) -> NDArray[Any]:
        raise NotImplementedError

    def set_value(self, name: str, values: NDArray[Any]) -> None:
        if not values.flags["C"]:
            raise InputError("Array should have C layout")
        var_type = self.get_var_type(name)
        var_type_lower = var_type.lower()
        if var_type_lower.startswith("double"):
            if values.dtype != np.float64:
                raise InputError("Array should have float64 elements")
            self._execute_function(
                self.lib.set_value,
                c_char_p(name.encode()),
                byref(values.ctypes.data_as(POINTER(c_double))),
            )
        elif var_type_lower.startswith("int"):
            if values.dtype != np.int32:
                raise InputError("Array should have int32 elements")
            self._execute_function(
                self.lib.set_value,
                c_char_p(name.encode()),
                byref(values.ctypes.data_as(POINTER(c_int))),
            )
        else:
            raise InputError("Unsupported value type")

    def set_value_at_indices(
        self, name: str, inds: NDArray[Any], src: NDArray[Any]
    ) -> None:
        raise NotImplementedError

    def get_grid_rank(self, grid: int) -> int:
        grid_rank = c_int(0)
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_rank,
            byref(c_grid),
            byref(grid_rank),
        )
        return grid_rank.value

    def get_grid_size(self, grid: int) -> int:
        grid_size = c_int(0)
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_size,
            byref(c_grid),
            byref(grid_size),
        )
        return grid_size.value

    def get_grid_type(self, grid: int) -> str:
        len_grid_type = self.get_constant_int("BMI_LENGRIDTYPE")
        grid_type = create_string_buffer(len_grid_type)
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_type,
            byref(c_grid),
            byref(grid_type),
        )
        return grid_type.value.decode()

    def get_grid_shape(self, grid: int, shape: NDArray[np.int32]) -> NDArray[np.int32]:
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_shape,
            byref(c_grid),
            c_void_p(shape.ctypes.data),
        )
        return shape

    def get_grid_spacing(
        self, grid: int, spacing: NDArray[np.int32]
    ) -> NDArray[np.int32]:
        raise NotImplementedError

    def get_grid_origin(
        self, grid: int, origin: NDArray[np.int32]
    ) -> NDArray[np.int32]:
        raise NotImplementedError

    def get_grid_x(self, grid: int, x: NDArray[np.float64]) -> NDArray[np.float64]:
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_x,
            byref(c_grid),
            c_void_p(x.ctypes.data),
        )
        return x

    def get_grid_y(self, grid: int, y: NDArray[np.float64]) -> NDArray[np.float64]:
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_y,
            byref(c_grid),
            c_void_p(y.ctypes.data),
        )
        return y

    def get_grid_z(self, grid: int, z: NDArray[np.float64]) -> NDArray[np.float64]:
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_z,
            byref(c_grid),
            c_void_p(z.ctypes.data),
        )
        return z

    def get_grid_node_count(self, grid: int) -> int:
        grid_node_count = c_int(0)
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_node_count,
            byref(c_grid),
            byref(grid_node_count),
        )
        return grid_node_count.value

    def get_grid_edge_count(self, grid: int) -> int:
        raise NotImplementedError

    def get_grid_face_count(self, grid: int) -> int:
        grid_face_count = c_int(0)
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_face_count,
            byref(c_grid),
            byref(grid_face_count),
        )
        return grid_face_count.value

    def get_grid_edge_nodes(
        self, grid: int, edge_nodes: NDArray[np.int32]
    ) -> NDArray[np.int32]:
        raise NotImplementedError

    def get_grid_face_edges(
        self, grid: int, face_edges: NDArray[np.int32]
    ) -> NDArray[np.int32]:
        raise NotImplementedError

    def get_grid_face_nodes(
        self, grid: int, face_nodes: NDArray[np.int32]
    ) -> NDArray[np.int32]:
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_face_nodes,
            byref(c_grid),
            c_void_p(face_nodes.ctypes.data),
        )
        return face_nodes

    def get_grid_nodes_per_face(
        self, grid: int, nodes_per_face: NDArray[np.int32]
    ) -> NDArray[np.int32]:
        c_grid = c_int(grid)
        self._execute_function(
            self.lib.get_grid_nodes_per_face,
            byref(c_grid),
            c_void_p(nodes_per_face.ctypes.data),
        )
        return nodes_per_face

    # ===========================
    # here starts the XMI
    # ===========================
    def prepare_time_step(self, dt: float) -> None:
        with cd(self.working_directory):
            c_dt = c_double(dt)
            self._execute_function(self.lib.prepare_time_step, byref(c_dt))

    def do_time_step(self) -> None:
        with cd(self.working_directory):
            self._execute_function(self.lib.do_time_step)

    def finalize_time_step(self) -> None:
        with cd(self.working_directory):
            self._execute_function(self.lib.finalize_time_step)

    def get_subcomponent_count(self) -> int:
        count = c_int(0)
        self._execute_function(self.lib.get_subcomponent_count, byref(count))
        return count.value

    def prepare_solve(self, component_id: int = 1) -> None:
        cid = c_int(component_id)
        with cd(self.working_directory):
            self._execute_function(self.lib.prepare_solve, byref(cid))

    def solve(self, component_id: int = 1) -> bool:
        cid = c_int(component_id)
        has_converged = c_int(0)
        with cd(self.working_directory):
            self._execute_function(self.lib.solve, byref(cid), byref(has_converged))
        return has_converged.value == 1

    def finalize_solve(self, component_id: int = 1) -> None:
        cid = c_int(component_id)

        with cd(self.working_directory):
            self._execute_function(self.lib.finalize_solve, byref(cid))

    def get_var_address(
        self, var_name: str, component_name: str, subcomponent_name: str = ""
    ) -> str:
        len_var_address = self.get_constant_int("BMI_LENVARADDRESS")
        var_address = create_string_buffer(len_var_address)
        self._execute_function(
            self.lib.get_var_address,
            c_char_p(component_name.upper().encode()),
            c_char_p(subcomponent_name.upper().encode()),
            c_char_p(var_name.upper().encode()),
            byref(var_address),
        )

        return var_address.value.decode()

    def _execute_function(
        self, function: Callable[[Any], int], *args: Any, **kwargs: Any
    ) -> None:
        """
        Utility function to execute a BMI function in the kernel and checks its status
        """

        if self.timing:
            self.timer.start(function.__name__)

        try:
            # Execute library function
            result = function(*args)

            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(
                    "execute function: %s returned %s",
                    repr_function_call(function.__name__, *args),
                    result,
                )

            if result != Status.SUCCESS:
                msg = "BMI exception in "
                msg += repr_function_call(function.__name__, *args)

                # try to get detailed error msg, beware:
                # directly call CDLL methods to avoid recursion
                try:
                    len_err_msg = self.get_constant_int("BMI_LENERRMESSAGE")
                    err_msg = create_string_buffer(len_err_msg)
                    self.lib.get_last_bmi_error(byref(err_msg))

                    len_name = self.get_constant_int("BMI_LENCOMPONENTNAME")
                    component_name = create_string_buffer(len_name)
                    self.lib.get_component_name(byref(component_name))

                    if "detail" in kwargs:
                        detail = f", details : '{kwargs['detail']}'"
                    else:
                        detail = ""
                    msg += (
                        f": Message from {component_name.value.decode()} "
                        + f"'{err_msg.value.decode()}'"
                        + detail
                    )
                except AttributeError:
                    self.logger.error("Couldn't extract error message")

                raise XMIError(msg)

        finally:
            if self.timing:
                self.timer.stop(function.__name__)

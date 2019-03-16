# Welcome to RAM and stack size hacks central, I'm your host, klardotsh!
# We really get stuck between a rock and a hard place on CircuitPython
# sometimes: our import structure is deeply nested enough that stuff
# breaks in some truly bizarre ways, including:
# - explicit RuntimeError exceptions, complaining that our
#   stack depth is too deep
#
# - silent hard locks of the device (basically unrecoverable without
#   UF2 flash if done in main.py, fixable with a reboot if done
#   in REPL)
#
# However, there's a hackaround that works for us! Because sys.modules
# caches everything it sees (and future imports will use that cached
# copy of the module), let's take this opportunity _way_ up the import
# chain to import _every single thing_ KMK eventually uses in a normal
# workflow, in order from fewest to least nested dependencies.

# First, stuff that has no dependencies, or only C/MPY deps
import collections  # isort:skip
import kmk.consts  # isort:skip
import kmk.kmktime  # isort:skip
import kmk.types  # isort:skip
import kmk.util  # isort:skip

import busio  # isort:skip

import supervisor  # isort:skip
from kmk.consts import LeaderMode, UnicodeMode  # isort:skip
from kmk.hid import USB_HID  # isort:skip
from kmk.internal_state import InternalState  # isort:skip
from kmk.keys import KC  # isort:skip
from kmk.matrix import MatrixScanner  # isort:skip

# Now handlers that will be used in keys later
import kmk.handlers.layers  # isort:skip
import kmk.handlers.stock  # isort:skip

# Now stuff that depends on the above (and so on)
import kmk.keys  # isort:skip
import kmk.matrix  # isort:skip

import kmk.hid  # isort:skip
import kmk.internal_state  # isort:skip

# GC runs automatically after CircuitPython imports.

# Thanks for sticking around. Now let's do real work, starting below

from kmk.util import intify_coordinate as ic
import busio
import gc

import supervisor
from kmk.consts import LeaderMode, UnicodeMode
from kmk.hid import USB_HID
from kmk.internal_state import InternalState
from kmk.keys import KC
from kmk.matrix import MatrixScanner
from kmk import led, rgb  # isort:skip


class Firmware:
    debug_enabled = False

    keymap = None
    coord_mapping = None

    row_pins = None
    col_pins = None
    diode_orientation = None
    matrix_scanner = MatrixScanner
    uart_buffer = []

    unicode_mode = UnicodeMode.NOOP
    tap_time = 300
    leader_mode = LeaderMode.TIMEOUT
    leader_dictionary = {}
    leader_timeout = 1000

    hid_helper = USB_HID

    # Split config
    extra_data_pin = None
    split_offsets = ()
    split_flip = False
    split_side = None
    split_type = None
    split_master_left = True
    is_master = None
    uart = None
    uart_flip = True
    uart_pin = None

    # RGB config
    rgb_pixel_pin = None
    rgb_config = rgb.rgb_config

    # led config (mono color)
    led_pin = None
    led_config = led.led_config

    def __init__(self):
        # Attempt to sanely guess a coord_mapping if one is not provided

        if not self.coord_mapping:
            self.coord_mapping = []

            rows_to_calc = len(self.row_pins)
            cols_to_calc = len(self.col_pins)

            if self.split_offsets:
                rows_to_calc *= 2
                cols_to_calc *= 2

            for ridx in range(rows_to_calc):
                for cidx in range(cols_to_calc):
                    self.coord_mapping.append(ic(ridx, cidx))

        self._state = InternalState(self)

    def _send_hid(self):
        self._hid_helper_inst.create_report(self._state.keys_pressed).send()
        self._state.resolve_hid()

    def _send_key(self, key):
        if not getattr(key, 'no_press', None):
            self._state.add_key(key)
            self._send_hid()

        if not getattr(key, 'no_release', None):
            self._state.remove_key(key)
            self._send_hid()

    def _handle_matrix_report(self, update=None):
        '''
        Bulk processing of update code for each cycle
        :param update:
        '''
        if update is not None:

            self._state.matrix_changed(
                update[0],
                update[1],
                update[2],
            )

    def _send_to_master(self, update):
        if self.split_master_left:
            update[1] += self.split_offsets[update[0]]
        else:
            update[1] -= self.split_offsets[update[0]]
        if self.uart is not None:
            self.uart.write(update)

    def _receive_from_slave(self):
        if self.uart is not None and self.uart.in_waiting > 0 or self.uart_buffer:
            if self.uart.in_waiting >= 60:
                # This is a dirty hack to prevent crashes in unrealistic cases
                import microcontroller
                microcontroller.reset()

            while self.uart.in_waiting >= 3:
                self.uart_buffer.append(self.uart.read(3))
            if self.uart_buffer:
                update = bytearray(self.uart_buffer.pop(0))

                # Built in debug mode switch
                if update == b'DEB':
                    print(self.uart.readline())
                    return None
                return update

        return None

    def _send_debug(self, message):
        '''
        Prepends DEB and appends a newline to allow debug messages to
        be detected and handled differently than typical keypresses.
        :param message: Debug message
        '''
        if self.uart is not None:
            self.uart.write('DEB')
            self.uart.write(message, '\n')

    def _master_half(self):
        if self.is_master is not None:
            return self.is_master

        # Working around https://github.com/adafruit/circuitpython/issues/1769
        try:
            self._hid_helper_inst.create_report([]).send()
            self.is_master = True
        except OSError:
            self.is_master = False

        return self.is_master

    def init_uart(self, pin, timeout=20):
        if self._master_half():
            return busio.UART(tx=None, rx=pin, timeout=timeout)
        else:
            return busio.UART(tx=pin, rx=None, timeout=timeout)

    def go(self):
        assert self.keymap, 'must define a keymap with at least one row'
        assert self.row_pins, 'no GPIO pins defined for matrix rows'
        assert self.col_pins, 'no GPIO pins defined for matrix columns'
        assert self.diode_orientation is not None, 'diode orientation must be defined'

        self._hid_helper_inst = self.hid_helper()

        # Split keyboard Init
        if self.split_flip and not self._master_half():
            self.col_pins = list(reversed(self.col_pins))

        if self.split_side == 'Left':
            self.split_master_left = self._master_half()
        elif self.split_side == 'Right':
            self.split_master_left = not self._master_half()

        if self.uart_pin is not None:
            self.uart = self.init_uart(self.uart_pin)

        if self.rgb_pixel_pin:
            self.pixels = rgb.RGB(self.rgb_config, self.rgb_pixel_pin)
            self.rgb_config = None  # No longer needed
        else:
            self.pixels = None

        if self.led_pin:
            self.led = led.led(self.led_pin, self.led_config)
            self.led_config = None  # No longer needed
        else:
            self.led = None

        self.matrix = MatrixScanner(
            cols=self.col_pins,
            rows=self.row_pins,
            diode_orientation=self.diode_orientation,
            rollover_cols_every_rows=getattr(self, 'rollover_cols_every_rows', None),
        )

        # Compile string leader sequences
        for k, v in self.leader_dictionary.items():
            if not isinstance(k, tuple):
                new_key = tuple(KC[c] for c in k)
                self.leader_dictionary[new_key] = v

        for k, v in self.leader_dictionary.items():
            if not isinstance(k, tuple):
                del self.leader_dictionary[k]

        if self.debug_enabled:
            print('Firin\' lazers. Keyboard is booted.')

        while True:
            state_changed = False

            if self.split_type is not None and self._master_half:
                update = self._receive_from_slave()
                if update is not None:
                    self._handle_matrix_report(update)
                    state_changed = True

            update = self.matrix.scan_for_changes()

            if update is not None:
                if self._master_half():
                    self._handle_matrix_report(update)
                    state_changed = True
                else:
                    # This keyboard is a slave, and needs to send data to master
                    self._send_to_master(update)

            if self._state.hid_pending:
                self._send_hid()

            old_timeouts_len = len(self._state.timeouts)
            self._state.process_timeouts()
            new_timeouts_len = len(self._state.timeouts)

            if old_timeouts_len != new_timeouts_len:
                state_changed = True

                if self._state.hid_pending:
                    self._send_hid()

            if self.debug_enabled and state_changed:
                print('New State: {}'.format(self._state._to_dict()))

            if self.debug_enabled and state_changed and self.pixels.enabled:
                print('New State: {}'.format(self.pixels))

            if self.pixels:
                # Only check animations if pixels is initialized
                if self.pixels.animation_mode:
                    if self.pixels.animation_mode:
                        self.pixels = self.pixels.animate()

            if self.led:
                # Only check animations if led is initialized
                if self.led.animation_mode:
                    self.led = self.led.animate()

            gc.collect()

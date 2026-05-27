"""
================================================================================
JD9168S_driver.py
Hardware driver for the 5.7" BOE TFT-LCD module (CDTECH S057BWV02NP-FC01)
running on the Jadard JD9168S controller at 640x480 RGB565.
Target: ESP32-S3 running the custom lvgl_micropython firmware.
================================================================================

WIRING (ESP32-S3 GPIO):
  RGB parallel bus:
    HSYNC=39  VSYNC=41  DE=40  PCLK=42
    B0-B4 (data0-4)  = 8,  3, 46,  9,  1
    G0-G5 (data5-10) = 5,  6,  7, 15, 16, 4
    R0-R4 (data11-15)= 45, 48, 47, 21, 14

  3-wire SPI (init only):
    SDA=17  SCL=18  CS=10

  Control pins:
    RESX (HW reset, active LOW)         = IO11
    STBYB (standby, HIGH = normal mode) = IO12
    TFT_POWER (LP3100 enable, HIGH = on)= IO13
    BACKLIGHT (PWM)                     = IO2

  GT911 capacitive touch (I2C):
    SDA=37  SCL=38  INT=35  RST=36
    I2C address 0x28 (per display datasheet section 1.2)

POWER-UP SEQUENCE (per datasheet 5.1 and supplied timing diagram):
    1. IO13 LOW -> HIGH       (LP3100 turns on AVDD/AVEE supplies)
    2. Wait for power rails to stabilise (~20 ms)
    3. IO12 LOW -> HIGH       (exit STBYB standby)
    4. IO11 pulse LOW >=20 us (HW reset), then HIGH
    5. Send the full JD9168S SPI init sequence over 3-wire SPI
    6. Start RGB bus and enable backlight

The JD9168S has NO D/C pin. 3-wire SPI uses 9-bit words where the first
(MSB) bit is the DCX flag: 0 = command byte, 1 = data byte.
================================================================================
"""

import machine
import lcd_bus
import rgb_display
import lvgl as lv
import gc
import time


class JD9168S_Display:
    """
    Hardware driver for the 5.7" JD9168S RGB display (640x480, BOE panel).
    Usage:
        from JD9168S_driver import JD9168S_Display
        display = JD9168S_Display()
        # LVGL is ready to use.
    """

    # Pin assignments                                                         
    PIN_HSYNC = 39
    PIN_VSYNC = 41
    PIN_DE = 40
    PIN_PCLK = 42

    # RGB565: data0-4 -> display B0-B4, data5-10 -> G0-G5, data11-15 -> R0-R4
    PIN_DATA = [
        8, 3, 46, 9, 1,            # Blue   (data0-4)
        5, 6, 7, 15, 16, 4,        # Green  (data5-10)
        45, 48, 47, 21, 14,        # Red    (data11-15)
    ]

    # 3-wire SPI for initialisation only
    PIN_SPI_SDA = 17
    PIN_SPI_SCL = 18
    PIN_SPI_CS = 10

    # Control pins
    PIN_RESX = 11
    PIN_STBYB = 12
    PIN_TFT_PWR = 13

    # GT911 capacitive touch controller (I2C)
    PIN_TOUCH_SDA = 37
    PIN_TOUCH_SCL = 38
    PIN_TOUCH_INT = 35
    PIN_TOUCH_RST = 36
    TOUCH_I2C_ADDR = 0x28       # per S057BWV02NP-FC01 spec section 1.2
    TOUCH_I2C_FREQ = 400_000    # GT911 standard fast-mode I2C

    # Display timings (from C-JD9168_BOE5_7_640RGBx480_RGB.dtsi)              
    PCLK_FREQ = 20_920_000         # 20.92 MHz
    HSYNC_PULSE_WIDTH = 20
    HSYNC_BACK_PORCH = 20
    HSYNC_FRONT_PORCH = 20
    VSYNC_PULSE_WIDTH = 4
    VSYNC_BACK_PORCH = 4
    VSYNC_FRONT_PORCH = 10

    # JD9168S initialisation sequence                                         
    # (cmd_byte, [data_bytes], delay_ms_after_command)                        
    INIT_SEQUENCE = (
        (0xDF, (0x91, 0x68, 0xF9), 0),
        (0xDE, (0x00,), 0),
        (0xB2, (0x00, 0x4A), 0),
        (0xBC, (0x2F, 0x34), 0),
        (0xC1, (0x00, 0x10, 0x00, 0x00, 0x00, 0x00), 0),
        (0xBB, (0x02, 0x24, 0x0A, 0x33, 0x10, 0x44, 0x44), 0),
        (0xBE, (0x1A, 0xF4), 0),
        (0xC3, (0x12, 0x2B, 0x99, 0x2B, 0x99, 0x05, 0x05, 0x05, 0x05,
                0x15, 0x15, 0x31, 0x85, 0x5D), 0),
        (0xC4, (0x02, 0xF0, 0xA0, 0x5E, 0x0A, 0x07, 0x14), 0),
        (0xCE, (0x00, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
                0x03, 0x03, 0x0F, 0x0F, 0x03, 0x03, 0x03, 0x03, 0x03,
                0x03, 0x03, 0x03, 0x03, 0x03), 0),
        (0xCF, (0x00, 0x00, 0x70, 0x01, 0x7C, 0x01, 0x7C, 0x3F, 0xFD,
                0x06, 0x7C, 0x00, 0x00), 0),
        (0xD0, (0x00, 0x1F, 0x1F, 0x1F, 0x1E, 0x1E, 0x15, 0x02, 0x00,
                0x17, 0x17, 0x17, 0x17, 0x1F, 0x1F, 0x06, 0x06, 0x04,
                0x04, 0x0A, 0x0A, 0x08, 0x08), 0),
        (0xD1, (0x00, 0x1F, 0x1F, 0x1F, 0x1E, 0x1E, 0x15, 0x03, 0x01,
                0x17, 0x17, 0x17, 0x17, 0x1F, 0x1F, 0x07, 0x07, 0x05,
                0x05, 0x0B, 0x0B, 0x09, 0x09), 0),
        (0xD2, (0x00, 0x1F, 0x1E, 0x1E, 0x1F, 0x1F, 0x15, 0x01, 0x03,
                0xD7, 0xD7, 0xD7, 0xD7, 0x1F, 0x1F, 0x09, 0x09, 0x0B,
                0x0B, 0x05, 0x05, 0x07, 0x07), 0),
        (0xD3, (0x00, 0x1F, 0x1E, 0x1E, 0x1F, 0x1F, 0x15, 0x00, 0x02,
                0xD7, 0xD7, 0xD7, 0xD7, 0x1F, 0x1F, 0x08, 0x08, 0x0A,
                0x0A, 0x04, 0x04, 0x06, 0x06), 0),
        (0xD4, (0x30, 0x00, 0x00, 0x06, 0x00, 0x08, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x03, 0x03, 0x00, 0x00, 0x80, 0x06, 0xC0,
                0x08, 0x03, 0x03, 0x11, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x01, 0x02, 0x05, 0x00, 0x00, 0x03, 0x04,
                0xC7, 0x03, 0xD5, 0x00, 0x00, 0x00, 0x03), 0),
        (0xD5, (0x68, 0x73, 0x00, 0x0A, 0x08, 0x00, 0x03, 0x00, 0x04,
                0x10, 0x03, 0x03, 0x02, 0xE3, 0x1C, 0xB3, 0x00, 0x00,
                0x00), 0),
        (0xB7, (0x00, 0xBF, 0x00, 0x00, 0xBF, 0x00), 0),
        (0xC8, (0x7C, 0x64, 0x54, 0x47, 0x42, 0x33, 0x38, 0x22, 0x3B,
                0x3B, 0x3C, 0x5B, 0x4B, 0x53, 0x44, 0x42, 0x32, 0x1C,
                0x06, 0x7C, 0x64, 0x54, 0x47, 0x42, 0x33, 0x38, 0x22,
                0x3B, 0x3B, 0x3C, 0x5B, 0x4B, 0x53, 0x44, 0x42, 0x32,
                0x1C, 0x06), 0),
        # --- Switch to register page 2 ---
        (0xDE, (0x02,), 0),
        (0xBB, (0x00, 0x5B, 0x5C, 0x41), 0),
        (0xC1, (0x15,), 0),
        (0xD0, (0x00, 0x66, 0x66, 0x02, 0x68, 0xAE, 0x14, 0x59, 0xCD,
                0x02, 0x68, 0xAE, 0x14, 0x59, 0xCD, 0x00), 0),
        (0xE7, (0x01,), 0),
        # --- Return to page 0 and start the panel ---
        (0xDE, (0x00,), 0),
        (0x11, (),       120),     # SLPOUT  -> wait 120 ms
        (0x29, (),        20),     # DISPON  -> wait 20 ms
    )

    # Construction                                                            
    def __init__(self, width=640, height=480, backlight_pin=2,
                 rgb565_byte_swap=True, enable_touch=True):
        """
        width, height     : panel resolution
        backlight_pin     : GPIO driving the backlight (datasheet IO2)
        rgb565_byte_swap  : if colours look wrong on first boot, toggle this.
                            Most ESP32-S3 + RGB565 panels need True; some
                            require False. Cannot be determined without
                            physical testing.
        enable_touch      : bring up the GT911 capacitive touch controller
                            and register it with LVGL as an input device.
        """
        self.width = width
        self.height = height
        self.backlight_pin = backlight_pin
        self.rgb565_byte_swap = rgb565_byte_swap
        self.enable_touch = enable_touch
        self.touch = None

        if not lv.is_initialized():
            lv.init()
            print("[JD9168S] LVGL core initialised.")

        self._setup_control_pins()
        self._power_up_sequence()
        self._spi_bitbang_init()
        self._init_rgb_bus()
        self._init_lvgl_display()
        self._enable_backlight()
        if self.enable_touch:
            self._init_touch()

        print("[JD9168S] Display ready.")

    #1 - GPIO setup                                                     
    def _setup_control_pins(self):
        # All control pins start LOW (safe / off state)
        self.tft_pwr = machine.Pin(self.PIN_TFT_PWR, machine.Pin.OUT, value=0)
        self.resx   = machine.Pin(self.PIN_RESX,    machine.Pin.OUT, value=0)
        self.stbyb  = machine.Pin(self.PIN_STBYB,   machine.Pin.OUT, value=0)
        self.bl     = machine.Pin(self.backlight_pin, machine.Pin.OUT, value=0)

        # 3-wire SPI: CS idle HIGH, SCL idle LOW, SDA idle LOW
        self.spi_cs  = machine.Pin(self.PIN_SPI_CS,  machine.Pin.OUT, value=1)
        self.spi_scl = machine.Pin(self.PIN_SPI_SCL, machine.Pin.OUT, value=0)
        self.spi_sda = machine.Pin(self.PIN_SPI_SDA, machine.Pin.OUT, value=0)

        print("[JD9168S] Control pins configured.")

    #2 - Power-up sequence                                              
    def _power_up_sequence(self):
        # 1. Ensure everything is in known-OFF state
        self.tft_pwr.value(0)
        self.resx.value(0)
        self.stbyb.value(0)
        time.sleep_ms(10)

        # 2. Enable the LP3100 power driver -> AVDD/AVEE ramp up
        #    Datasheet tRamp1+tRamp2+tRamp3 = up to ~20 ms worst case
        self.tft_pwr.value(1)
        time.sleep_ms(20)

        # 3. Leave standby mode
        self.stbyb.value(1)
        time.sleep_ms(5)

        # 4. Hardware reset pulse
        #    Datasheet: RESX LOW >= 20 us, then HIGH. We use 10 ms for
        #    noise immunity, still well within the max.
        self.resx.value(1)
        time.sleep_ms(1)
        self.resx.value(0)
        time.sleep_ms(10)
        self.resx.value(1)

        # 5. Wait t1 (>= 5 ms) before sending any SPI command
        time.sleep_ms(10)

        print("[JD9168S] Power-up sequence complete.")

    #3 - 9-bit bit-banged SPI init                                      
    def _spi_write_9bit(self, byte_val, is_data):
        """
        Transmit one 9-bit word.
        Bit 8 (first clocked out) = DCX (0=cmd, 1=data)
        Bits 7..0                 = payload byte, MSB first
        Mode 0: SCL idles LOW, data sampled on the rising edge.
        """
        # DCX bit
        self.spi_sda.value(1 if is_data else 0)
        self.spi_scl.value(0)
        self.spi_scl.value(1)

        # 8 payload bits, MSB first
        for shift in (7, 6, 5, 4, 3, 2, 1, 0):
            self.spi_sda.value((byte_val >> shift) & 1)
            self.spi_scl.value(0)
            self.spi_scl.value(1)

    def _spi_send_command(self, cmd, data_bytes):
        """
        One command frame: CS falls, DCX=0 command byte, DCX=1 for each
        data byte, CS rises. Python GPIO calls on the ESP32-S3 naturally
        run around tens of kHz, well inside the JD9168S timing budget;
        no explicit delays required.
        """
        self.spi_cs.value(0)
        self._spi_write_9bit(cmd, is_data=False)
        for byte in data_bytes:
            self._spi_write_9bit(byte, is_data=True)
        self.spi_cs.value(1)

    def _spi_bitbang_init(self):
        print("[JD9168S] Sending SPI init sequence...")
        for cmd, data, delay_ms in self.INIT_SEQUENCE:
            self._spi_send_command(cmd, data)
            if delay_ms:
                time.sleep_ms(delay_ms)
        print("[JD9168S] Init sequence complete ({} commands sent).".format(
            len(self.INIT_SEQUENCE)))

    #4 - ESP32-S3 RGB peripheral + framebuffers                         
    def _init_rgb_bus(self):
        bus_kwargs = {
            'hsync': self.PIN_HSYNC,
            'vsync': self.PIN_VSYNC,
            'de':    self.PIN_DE,
            'pclk':  self.PIN_PCLK,
            'data0':  self.PIN_DATA[0],  'data1':  self.PIN_DATA[1],
            'data2':  self.PIN_DATA[2],  'data3':  self.PIN_DATA[3],
            'data4':  self.PIN_DATA[4],  'data5':  self.PIN_DATA[5],
            'data6':  self.PIN_DATA[6],  'data7':  self.PIN_DATA[7],
            'data8':  self.PIN_DATA[8],  'data9':  self.PIN_DATA[9],
            'data10': self.PIN_DATA[10], 'data11': self.PIN_DATA[11],
            'data12': self.PIN_DATA[12], 'data13': self.PIN_DATA[13],
            'data14': self.PIN_DATA[14], 'data15': self.PIN_DATA[15],
            'freq':               self.PCLK_FREQ,
            'hsync_pulse_width':  self.HSYNC_PULSE_WIDTH,
            'hsync_back_porch':   self.HSYNC_BACK_PORCH,
            'hsync_front_porch':  self.HSYNC_FRONT_PORCH,
            'vsync_pulse_width':  self.VSYNC_PULSE_WIDTH,
            'vsync_back_porch':   self.VSYNC_BACK_PORCH,
            'vsync_front_porch':  self.VSYNC_FRONT_PORCH,
        }
        self.bus = lcd_bus.RGBBus(**bus_kwargs)
        print("[JD9168S] RGB bus configured.")

        # Double framebuffers in PSRAM.
        # 640 x 480 x 2 bytes = 614,400 bytes per buffer (~600 KB).
        # 8 MB Octal PSRAM easily fits two; 16 MB variants leave plenty free.
        buf_size = self.width * self.height * 2
        gc.collect()

        self.fb1 = self.bus.allocate_framebuffer(buf_size,
                                                 lcd_bus.MEMORY_SPIRAM)
        self.fb2 = self.bus.allocate_framebuffer(buf_size,
                                                 lcd_bus.MEMORY_SPIRAM)
        print("[JD9168S] Two PSRAM framebuffers allocated "
              "({} bytes each).".format(buf_size))

    #5 - Bind to LVGL                                                   
    def _init_lvgl_display(self):
        # The binding's keyword evolved between releases: newer builds use
        # `color_space`, some older ones use `color_format`. Try both.
        common_kwargs = {
            'data_bus':        self.bus,
            'display_width':   self.width,
            'display_height':  self.height,
            'frame_buffer1':   self.fb1,
            'frame_buffer2':   self.fb2,
            'rgb565_byte_swap': self.rgb565_byte_swap,
        }
        try:
            self.display = rgb_display.RGBDisplay(
                color_space=lv.COLOR_FORMAT.RGB565, **common_kwargs)
        except TypeError:
            self.display = rgb_display.RGBDisplay(
                color_format=lv.COLOR_FORMAT.RGB565, **common_kwargs)

        self.display.init()
        print("[JD9168S] LVGL display bound.")

    #6 - Backlight                                                      #
    def _enable_backlight(self):
        # Datasheet tBLON >= 2 ms after DISPON
        time.sleep_ms(5)
        self.bl.value(1)
        print("[JD9168S] Backlight on.")

    #7 - GT911 capacitive touch                                         
    def _init_touch(self):
        """
        Bring up the GT911 capacitive touch controller and register it
        with LVGL as an input device.

        The firmware ships two pieces needed here:
          - `i2c`  : a small I2C bus/device helper
          - `gt911`: the GT911 driver, which is itself an LVGL indev

        Per the display datasheet (section 1.2) this panel's GT911 responds
        at I2C address 0x28. The firmware's gt911 module performs the
        standard address-select reset dance (INT held LOW during RST rise
        selects 0x5D; INT held HIGH selects 0x28), so we pass the address
        through and let the driver handle the timing.
        """
        try:
            from i2c import I2C
            import gt911
        except ImportError as e:
            print("[JD9168S] Touch disabled - missing firmware module: {}"
                  .format(e))
            return

        try:
            i2c_bus = I2C.Bus(
                host=1,
                scl=self.PIN_TOUCH_SCL,
                sda=self.PIN_TOUCH_SDA,
                freq=self.TOUCH_I2C_FREQ,
                use_locks=False,
            )
            touch_dev = I2C.Device(
                i2c_bus,
                dev_id=self.TOUCH_I2C_ADDR,
                reg_bits=gt911.BITS,
            )

            self.touch = gt911.GT911(
                touch_dev,
                reset_pin=self.PIN_TOUCH_RST,
                interrupt_pin=self.PIN_TOUCH_INT,
            )

            # Match LVGL's coordinate system to the panel; the GT911 reports
            # raw panel pixels so no scaling is needed at this resolution.
            try:
                self.touch.set_resolution(self.width, self.height)
            except AttributeError:
                pass

            print("[JD9168S] GT911 touch controller ready "
                  "(I2C 0x{:02X} on SDA={}, SCL={}).".format(
                      self.TOUCH_I2C_ADDR,
                      self.PIN_TOUCH_SDA, self.PIN_TOUCH_SCL))
        except Exception as e:
            print("[JD9168S] Touch init failed: {}".format(e))
            self.touch = None

    # Public helpers                                                          
    def set_backlight(self, percentage):
        """
        Set backlight brightness. 0 = off, 100 = full on.
        If the underlying display object supports PWM dimming it will be
        used; otherwise this falls back to a plain on/off toggle.
        """
        if not 0 <= percentage <= 100:
            raise ValueError("Backlight percentage must be between 0 and 100.")
        try:
            self.display.set_backlight(percentage)
        except (AttributeError, NotImplementedError):
            self.bl.value(1 if percentage > 0 else 0)

    def debug_memory(self):
        """Print free RAM / PSRAM. Useful when tuning LVGL buffer sizes."""
        gc.collect()
        free_ram = gc.mem_free()
        free_psram = 0
        try:
            import esp32
            free_psram = esp32.psram_mem_free()
        except (ImportError, AttributeError):
            pass
        print("[JD9168S] Free RAM: {:,} bytes | "
              "Free PSRAM: {:,} bytes".format(free_ram, free_psram))
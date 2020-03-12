# Written by Sultan Qasim Khan
# Copyright (c) 2019, NCC Group plc
# Released as open source under GPLv3

from serial import Serial
from struct import pack, unpack
from base64 import b64encode, b64decode
from binascii import Error as BAError
from sys import stderr
from time import time
from enum import Enum
from random import randint
from traceback import print_exc

class SniffleHW:
    def __init__(self, serport):
        self.decoder_state = SniffleDecoderState()
        self.ser = Serial(serport, 2000000)
        self.ser.write(b'@@@@@@@@\r\n') # command sync
        self.recv_cancelled = False

    def _send_cmd(self, cmd_byte_list):
        b0 = (len(cmd_byte_list) + 3) // 3
        cmd = bytes([b0, *cmd_byte_list])
        msg = b64encode(cmd) + b'\r\n'
        self.ser.write(msg)

    def cmd_chan_aa_phy(self, chan=37, aa=0x8E89BED6, phy=0, crci=0x555555):
        if not (0 <= chan <= 39):
            raise ValueError("Channel must be between 0 and 39")
        if not (0 <= phy <= 2):
            raise ValueError("PHY must be 0 (1M), 1 (2M), or 2 (coded)")
        self._send_cmd([0x10, *list(pack("<BLBL", chan, aa, phy, crci))])

    def cmd_pause_done(self, pause_when_done=False):
        if pause_when_done:
            self._send_cmd([0x11, 0x01])
        else:
            self._send_cmd([0x11, 0x00])

    def cmd_rssi(self, rssi=-80):
        self._send_cmd([0x12, rssi & 0xFF])

    def cmd_mac(self, mac_byte_list=None, hop3=True):
        if mac_byte_list is None:
            self._send_cmd([0x13])
        else:
            if len(mac_byte_list) != 6:
                raise ValueError("MAC must be 6 bytes!")
            self._send_cmd([0x13, *mac_byte_list])
            if hop3:
                # hop with advertisements between 37/38/39
                # unnecessary/detrimental with extended advertising
                self._send_cmd([0x14])

    def cmd_endtrim(self, end_trim=0x10):
        self._send_cmd([0x15, *list(pack("<L", end_trim))])

    def cmd_auxadv(self, enable=True):
        if enable:
            self._send_cmd([0x16, 0x01])
        else:
            self._send_cmd([0x16, 0x00])

    def cmd_reset(self):
        self._send_cmd([0x17])

    def cmd_marker(self):
        self._send_cmd([0x18])

    # for master or slave modes
    def cmd_transmit(self, llid, pdu):
        if not (0 <= llid <= 3):
            raise ValueError("Out of bounds LLID")
        if len(pdu) > 255:
            raise ValueError("Too long PDU")
        self._send_cmd([0x19, llid, len(pdu), *pdu])

    def cmd_connect(self, peerAddr, llData):
        if len(peerAddr) != 6:
            raise ValueError("Invalid peer address")
        if len(llData) != 22:
            raise ValueError("Invalid LLData")
        self._send_cmd([0x1A, *peerAddr, *llData])

    def cmd_setaddr(self, addr):
        if len(addr) != 6:
            raise ValueError("Invalid MAC address")
        self._send_cmd([0x1B, *addr])

    def cmd_advertise(self, advData, scanRspData):
        if len(advData) > 31:
            raise ValueError("advData too long!")
        if len(scanRspData) > 31:
            raise ValueError("scanRspData too long!")
        paddedAdvData = [len(advData), *advData] + [0]*(31 - len(advData))
        paddedScnData = [len(scanRspData), *scanRspData] + [0]*(31 - len(scanRspData))
        self._send_cmd([0x1C, *paddedAdvData, *paddedScnData])

    def cmd_adv_interval(self, intervalMs):
        if not (20 < intervalMs < 0xFFFF):
            raise ValueError("Advertising interval out of bounds")
        self._send_cmd([0x1D, intervalMs & 0xFF, intervalMs >> 8])

    def cmd_irk(self, irk=None, hop3=True):
        if irk is None:
            self._send_cmd([0x1E])
        elif len(irk) != 16:
            raise ValueError("Invalid IRK length!")
        else:
            self._send_cmd([0x1E, *irk])
            if hop3:
                self._send_cmd([0x14])

    def recv_msg(self):
        got_msg = False
        while not got_msg:
            pkt = self.ser.readline()
            try:
                data = b64decode(pkt.rstrip())
            except BAError as e:
                print(str(pkt, encoding='ascii').rstrip())
                print("Ignoring message:", e, file=stderr)
                continue
            got_msg = True

        if self.recv_cancelled:
            self.recv_cancelled = False
            return -1, None, b''

        # msg type, msg body
        return data[0], data[1:], pkt

    def recv_and_decode(self):
        mtype, mbody, pkt = self.recv_msg()
        try:
            if mtype == 0x10:
                return PacketMessage(mbody, self.decoder_state)
            elif mtype == 0x11:
                return DebugMessage(mbody)
            elif mtype == 0x12:
                return MarkerMessage(mbody, self.decoder_state)
            elif mtype == 0x13:
                return StateMessage(mbody, self.decoder_state)
            elif mtype == -1:
                return None # receive cancelled
            else:
                raise SniffleHWPacketError("Unknown message type 0x%02X!" % mtype)
        except BaseException as e:
            print(str(pkt, encoding='ascii').rstrip())
            print("Ignoring message:", e, file=stderr)
            print_exc()
            return None

    def cancel_recv(self):
        self.recv_cancelled = True
        self.ser.cancel_read()

    def mark_and_flush(self):
        # use marker to zero time, flush every packet before marker
        # also tolerate errors from incomplete lines in UART buffer
        self.cmd_marker()
        while True:
            try:
                msg = self.recv_and_decode()
            except SniffleHWPacketError:
                print("WARNING: invalid message during flush, ignoring...")
                continue
            if isinstance(msg, MarkerMessage):
                break

    def random_addr(self):
        # generate a random static address, set it
        addr = [randint(0, 255) for i in range(6)]
        addr[5] |= 0xC0 # make it static
        self.cmd_setaddr(bytes(addr))

    # automatically generate sane LLData
    def initiate_conn(self, peerAddr):
        llData = []

        # access address
        llData.extend([randint(0, 255) for i in range(4)])

        # initial CRC
        llData.extend([randint(0, 255) for i in range(3)])

        # WinSize, WinOffset, Interval, Latency, Timeout
        llData.append(3)
        llData.extend(pack("<H", randint(5, 15)))
        llData.extend(pack("<H", 24))
        llData.extend(pack("<H", 1))
        llData.extend(pack("<H", 50))

        # Channel Map
        llData.extend([0xFF, 0xFF, 0xFF, 0xFF, 0x1F])

        # Hop, SCA = 0
        llData.append(randint(5, 16))

        self.cmd_connect(peerAddr, bytes(llData))

        # return the access address
        return unpack("<L", bytes(llData[:4]))[0]

# raised when sniffle HW gives invalid data (shouldn't happen)
# this is not for malformed Bluetooth traffic
class SniffleHWPacketError(ValueError):
    pass

BLE_ADV_AA = 0x8E89BED6

class SniffleDecoderState:
    def __init__(self):
        # packet receive time tracking
        self.time_offset = 1
        self.first_epoch_time = 0
        self.ts_wraps = 0
        self.last_ts = -1

        # access address tracking
        self.cur_aa = BLE_ADV_AA

        # state tracking
        self.last_state = SnifferState.STATIC

# radio time wraparound period in seconds
TS_WRAP_PERIOD = 0x100000000 / 4E6

class PacketMessage:
    def __init__(self, raw_msg, dstate):
        ts, l, rssi, chan = unpack("<LBbB", raw_msg[:7])
        body = raw_msg[7:]

        if len(body) != l:
            raise SniffleHWPacketError("Incorrect length field!")

        phy = chan >> 6
        chan &= 0x3F

        if chan >= 37 and dstate.cur_aa != BLE_ADV_AA:
            dstate.cur_aa = BLE_ADV_AA

        if dstate.time_offset > 0:
            dstate.first_epoch_time = time()
            dstate.time_offset = ts / -1000000.

        if ts < dstate.last_ts:
            dstate.ts_wraps += 1
        dstate.last_ts = ts

        real_ts = dstate.time_offset + (ts / 1000000.) + (dstate.ts_wraps * TS_WRAP_PERIOD)
        real_ts_epoch = dstate.first_epoch_time + real_ts

        # Now actually set instance attributes
        self.ts = real_ts
        self.ts_epoch = real_ts_epoch
        self.aa = dstate.cur_aa
        self.rssi = rssi
        self.chan = chan
        self.phy = phy
        self.body = body

    def __repr__(self):
        return "%s(ts=%.6f, aa=%08X, rssi=%d, chan=%d, phy=%d, body=%s)" % (
                type(self).__name__, self.ts, self.aa, self.rssi, self.chan, self.phy, repr(self.body))

    def str_header(self):
        phy_names = ["1M", "2M", "Coded", "Reserved"]
        return "Timestamp: %.6f\tLength: %i\tRSSI: %i\tChannel: %i\tPHY: %s" % (
            self.ts, len(self.body), self.rssi, self.chan, phy_names[self.phy])

    def __str__(self):
        return self.str_header()

class DebugMessage:
    def __init__(self, raw_msg):
        self.msg = str(raw_msg, encoding='latin-1')

    def __repr__(self):
        return "%s(msg=%s)" % (type(self).__name__, repr(self.msg))

    def __str__(self):
        return "DEBUG: " + self.msg

class MarkerMessage:
    def __init__(self, raw_msg, dstate):
        ts, = unpack("<L", raw_msg)

        # these messages are intended to mark the zero time
        dstate.first_epoch_time = time()
        dstate.time_offset = ts / -1000000.

# TODO: have a state enum in Python

class SnifferState(Enum):
    STATIC = 0
    ADVERT_SEEK = 1
    ADVERT_HOP = 2
    DATA = 3
    PAUSED = 4
    INITIATING = 5
    MASTER = 6
    SLAVE = 7
    ADVERTISING = 8
    SCANNING = 9

class StateMessage:
    def __init__(self, raw_msg, dstate):
        self.last_state = dstate.last_state
        self.new_state = SnifferState(raw_msg[0])
        dstate.last_state = self.new_state

    def __repr__(self):
        return "%s(new=%s, old=%s)" % (type(self).__name__,
                str(self.new_state), str(self.last_state))

    def __str__(self):
        return "TRANSITION: %s from %s" % (str(self.new_state),
                str(self.last_state))

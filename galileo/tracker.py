
from ctypes import c_byte

import logging
logger = logging.getLogger(__name__)

from .crc import CRC16
from .dongle import TimeoutError, DM
from .utils import a2x, i2lsba, a2lsbi

MICRODUMP = 3
MEGADUMP = 13

def unSLIP1(data):
    """ The protocol uses a particular version of SLIP (RFC 1055) applied
    only on the first byte of the data"""
    END = 0300
    ESC = 0333
    ESC_ = {0334: END,
            0335: ESC}
    if data[0] == ESC:
        return [ESC_[data[1]]] + data[2:]
    return data

class Tracker(object):
    def __init__(self, Id, addrType, attributes, serviceUUID=None):
        self.id = Id
        self.addrType = addrType
        if serviceUUID is None:
            self.serviceUUID = [Id[1] ^ Id[3] ^ Id[5], Id[0] ^ Id[2] ^ Id[4]]
        else:
            self.serviceUUID = serviceUUID
        self.attributes = attributes

    @property
    def syncedRecently(self):
        return self.attributes[1] != 4


class FitbitClient(object):
    def __init__(self, dongle):
        self.dongle = dongle

    def disconnect(self):
        logger.info('Disconnecting from any connected trackers')

        self.dongle.ctrl_write([2, 2])
        self.dongle.ctrl_read()  # CancelDiscovery
        self.dongle.ctrl_read()  # TerminateLink

        try:
            # It is OK to have a timeout with the following ctrl_read as
            # they are there to clean up any connection left open from
            # the previous attempts.
            self.dongle.ctrl_read()
            self.dongle.ctrl_read()
            self.dongle.ctrl_read()
        except TimeoutError:
            # assuming link terminated
            pass

    def getDongleInfo(self):
        try:
            self.dongle.ctrl_write([2, 1, 0, 0x78, 1, 0x96])
            d = self.dongle.ctrl_read()
            self.major = d[2]
            self.minor = d[3]
            logger.debug('Fitbit dongle version major:%d minor:%d', self.major,
                         self.minor)
        except TimeoutError:
            logger.error('Failed to get connected Fitbit dongle information')
            raise

    def discover(self, minDuration=4000):
        self.dongle.ctrl_write([0x1a, 4, 0xba, 0x56, 0x89, 0xa6, 0xfa, 0xbf,
                                0xa2, 0xbd, 1, 0x46, 0x7d, 0x6e, 0, 0,
                                0xab, 0xad, 0, 0xfb, 1, 0xfb, 2, 0xfb] +
                               i2lsba(minDuration, 2) + [0, 0xd3, 0, 0, 0, 0])
        self.dongle.ctrl_read()  # StartDiscovery
        d = self.dongle.ctrl_read(minDuration)
        while d[0] != 3:
            trackerId = list(d[2:8])
            addrType = d[8]
            RSSI = c_byte(d[9]).value
            attributes = list(d[11:13])
            sUUID = list(d[17:19])
            serviceUUID = [trackerId[1] ^ trackerId[3] ^ trackerId[5],
                           trackerId[0] ^ trackerId[2] ^ trackerId[4]]
            tracker = Tracker(trackerId, addrType, attributes, sUUID)
            if not tracker.syncedRecently and (serviceUUID != sUUID):
                logger.error("Error in communication to tracker %s, cannot acknowledge the serviceUUID: %s vs %s",
                             a2x(trackerId, delim=""), a2x(serviceUUID, ':'), a2x(sUUID, ':'))
            logger.debug('Tracker: %s, %s, %s, %s', a2x(trackerId, ':'), addrType, RSSI, a2x(attributes, ':'))
            if RSSI < -80:
                logger.info("Tracker %s has low signal power (%ddBm), higher"
                            " chance of miscommunication",
                            a2x(trackerId, delim=""), RSSI)
            if not tracker.syncedRecently:
                logger.debug('Tracker %s was not recently synchronized', a2x(trackerId, delim=""))
            yield tracker
            d = self.dongle.ctrl_read(minDuration)

        # tracker found, cancel discovery
        self.dongle.ctrl_write([2, 5])
        self.dongle.ctrl_read()  # CancelDiscovery

    def establishLink(self, tracker):
        self.dongle.ctrl_write([0xb, 6] + tracker.id + [tracker.addrType] + tracker.serviceUUID)
        self.dongle.ctrl_read()  # EstablishLink
        self.dongle.ctrl_read(5000)
        # established, waiting for service discovery
        # - This one takes long
        self.dongle.ctrl_read(8000)  # GAP_LINK_ESTABLISHED_EVENT
        self.dongle.ctrl_read()

    def enableTxPipe(self):
        # enabling tx pipe
        self.dongle.ctrl_write([3, 8, 1])
        self.dongle.data_read(5000)

    def initializeAirlink(self):
        self.dongle.data_write(DM([0xc0, 0xa, 0xa, 0, 6, 0, 6, 0, 0, 0, 0xc8, 0]))
        self.dongle.ctrl_read(10000)
        self.dongle.data_read()

    def getDump(self, dumptype=MEGADUMP):
        logger.debug('Getting dump type %d', dumptype)

        # begin dump of appropriate type
        self.dongle.data_write(DM([0xc0, 0x10, dumptype]))
        r = self.dongle.data_read()
        assert r.data == [0xc0, 0x41, dumptype], r.data

        dump = []
        crc = CRC16()
        # megadump body
        d = self.dongle.data_read()
        dump.extend(d.data)
        crc.update(d.data)
        while d.data[0] != 0xc0:
            d = self.dongle.data_read()
            dump.extend(unSLIP1(d.data))
            if d.data[0] != 0xc0:
                crc.update(unSLIP1(d.data))
        # megadump footer
        dataType = d.data[2]
        assert dataType == dumptype, "%x != %x" % (dataType, dumptype)
        nbBytes = a2lsbi(d.data[5:7])
        transportCRC = a2lsbi(d.data[3:5])
        esc1 = d.data[7]
        esc2 = d.data[8]
        dumpLen = len(dump) - d.len
        if dumpLen != nbBytes:
            logger.error("Error in communication, Expected length: %d bytes,"
                         " received %d bytes", nbBytes, dumpLen)
        crcVal = crc.final()
        if transportCRC != crcVal:
            logger.error("error in communication, Expected CRC: 0x%04X,"
                         " received 0x%04X", crcVal, transportCRC)
        logger.debug('Dump done, length %d', nbBytes)
        logger.debug('transportCRC=0x%04x, esc1=0x%02x, esc2=0x%02x', transportCRC, esc1, esc2)
        return dump

    def uploadResponse(self, response):
        self.dongle.data_write(DM([0xc0, 0x24, 4] + i2lsba(len(response), 2) + [0, 0, 0, 0]))
        self.dongle.data_read()

        for i in range(0, len(response), 20):
            self.dongle.data_write(DM(response[i:i + 20]))
            self.dongle.data_read()

        self.dongle.data_write(DM([0xc0, 2]))
        self.dongle.data_read(60000)  # This one can be very long. He is probably erasing the memory there
        self.dongle.data_write(DM([0xc0, 1]))
        self.dongle.data_read()

    def disableTxPipe(self):
        self.dongle.ctrl_write([3, 8])
        self.dongle.data_read(5000)

    def terminateAirlink(self):
        self.dongle.ctrl_write([2, 7])
        self.dongle.ctrl_read()  # TerminateLink

        self.dongle.ctrl_read()
        self.dongle.ctrl_read()  # GAP_LINK_TERMINATED_EVENT
        self.dongle.ctrl_read()  # 22
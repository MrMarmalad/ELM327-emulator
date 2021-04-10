#!/usr/bin/env python3
# -*- coding: utf-8 -*-
###########################################################################
# ELM327-emulator
# TASK PLUGIN: UDS write_vin
# ELM327 Emulator for testing software interfacing OBDII via ELM327 adapter
# https://github.com/Ircama/ELM327-emulator
# (C) Ircama 2021 - CC-BY-NC-SA-4.0
###########################################################################

from elm import Tasks

# UDS - MODE 2E - writeDataByIdentifier Service (Appl. Inc.)
# F190, write_VIN
class Task(Tasks):
    def run(self, length, frame, cmd):
        ret = self.multiline_request(length, frame, cmd)
        if ret is False or ret is None:
            return ret
        if ret[:6] == '2EF190': # Write VIN
            self.logging.warning('Decoded VIN: %s',
                                 bytearray.fromhex(ret[6:]).decode())
        else:
            self.logging.error('Invalid data %s', self.req)
            return self.ST('NO DATA'), self.TASK_TERMINATE
        return (self.HD(self.answer) + self.SZ('03') +
                self.DT('6E F1 90'), # WDBI message-SF response
                self.TASK_TERMINATE)
from __future__ import print_function

import sys
import pika, os

from model import Model
from scheduler_state import SchedulerState
from simulator import Simulator
from threading import Thread
from socket import *
from struct import pack
from numpy import array
from pygame.time import Clock
from rabbit import CHANNEL


__all__ = ['Frontage']

def print_flush(s):
    print(s)
    sys.stdout.flush()


class Frontage(Thread):
    def __init__(self, hardware_port, hardware=True):
        Thread.__init__(self)
        self.model = Model(4, 19)
        self.clock = Clock()

        # row, column -> DMX address
        self.mapping = array([[19, 18, 17, 16, 15, 14, 13, 12, 11, 10,  9,  8,  7,  6,  5,  4,  3, 2,  1],
                              [38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20],
                              [57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39],
                              [71, 70, 69, 68, 67, 66, 65,  0,  0,  0,  0,  0, 64, 63, 62, 61, 60, 59, 58]])


        self.num_pixels = self.mapping.shape[0] * self.mapping.shape[1]
        # Use asyncio or twisted?
        self.hardware_server = socket(AF_INET, SOCK_STREAM)
        #self.hardware_server.settimeout(10.0)  # Non-blocking requests
        self.hardware_server.bind(("0.0.0.0", 33460))
        self.hardware_server.listen(1)
        # self.start_rabbit()

        if hardware:
            print_flush("Waiting Hardware TCP client connection...")
            print_flush(hardware_port)
            self.client, self.address = self.hardware_server.accept()
            print_flush("Client {}:{} connected!".format(self.address[0], self.address[1]))
        else:
            self.client, self.address = None, None

    def map(self, row, column):
        return self.mapping[row][column]

    def __getitem__(self, row):
        return self.model.__getitem__(row)

    def __setitem__(self, key, value):
        with self.model:
            self.model.__setitem__(key, value)

    def set_all(self, r, g, b):
        for row in range(self.model.height):
            for col in range(self.model.width):
                self.model[row, col] = r, g, b

    def erase_all(self):
        self.set_all(0, 0, 0)

    def handle_model_msg(self, channel, method, properties, body):
        print('[MODEL-QUEUE] Received data')
        self.model.set_from_json(body)
        self.update()

    def run(self):
        print("==> Frontage Controler Started")
        try:
            CHANNEL.basic_consume(self.handle_model_msg,
              queue=SchedulerState.KEY_MODEL,
              no_ack=True)

            # start consuming (blocks)
            CHANNEL.start_consuming()
        finally:
            self.close()

    def update(self):
        if self.client is not None:
            with self.model:
                data_frame = []
                with self.model:
                    for row in range(self.model.height):
                        for col in range(self.model.width):
                            led = self.mapping[row, col]
                            r, g, b = self.model[row, col]
                            data_frame.append(led)
                            data_frame.append(r)
                            data_frame.append(g)
                            data_frame.append(b)
                command = pack("!{}B".format(self.num_pixels * 4), *data_frame)
                self.client.send(command)

    def close(self):
        print("==> Frontage Controler Ended. ByeBye")
        if self.hardware_server is not None:
            self.hardware_server.close()
        # if RABBIT_CONNECTION:
        #      RABBIT_CONNECTION.close()


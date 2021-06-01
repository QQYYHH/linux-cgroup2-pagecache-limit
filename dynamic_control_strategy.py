#!/usr/bin/python
# -*- coding: utf-8 -*-
import os
import time
import argparse

'''
the output format of vmstat
procs -----------------------memory---------------------- ---swap-- -----io---- -system-- --------cpu--------
 r  b         swpd         free         buff        cache   si   so    bi    bo   in   cs  us  sy  id  wa  st
 0  0           86         2432          331         2795    0    0     5     4    2    1   0   0 100   0   0

 bi - row2 col8
 so - row2 col7
 bo - row2 col9
'''


# the fluctuation threashold of different index
bi_threashold = 20000
so_threashold = 100
bo_threashold = 50000

# cgroup directory path
cgroup_path = '/sys/fs/cgroup/interfere'

# cgroup resource usage upper limit
# unit is B
pagecache = '4294967296'
memory = '2147483648'
io = '8:0 rbps=1M wbps=1M'

# get indexs(bi, so, bo) though vmstat
# ret: array of bi, so and bo
def get_index():
    output = os.popen('vmstat -w -S M', 'r')
    line = output.readlines()[2]
    result = [int(val) for val in line.split()]
    ret = [result[8], result[7], result[9]]
    return ret


# compare whether current index exceeds the corresponding threashold
# param
# @index_array the index array return from function get_index()
# @bi_lst previous value of bi
# @so_lst previous value of so
# @bo_lst previous value of bo
# ret
# which index has been exceeded
# 0 - bi, 1 - so, 2 - bo
# -1 if none
def cmp_threashold(index_array, bi_lst, so_lst, bo_lst):
    bi = index_array[0]
    so = index_array[1]
    bo = index_array[2]
    global bi_threashold
    global so_threashold
    global bo_threashold
    if bi - bi_lst > bi_threashold:
        return 0
    elif so - so_lst > so_threashold:
        return 1
    elif bo - bo_lst > bo_threashold:
        return 2
    return -1


# handle exceed event
# param
# @index which index has been exceeded
# 0 - bi, 1 - so, 2 - bo
def handle_exceed(index):
    # if 0
    # bi fluctuate that is page cache is under much pressure
    # so we limit the page cache usage of interfere program
    # if 1
    # so fluctuate that is memory is under much pressure
    # so we limit the memory usage of interfere program
    # if 2
    # bo fluctuate that is io is under much pressure
    # so we limit the io usage of interfere program

    os.system('cd ' + cgroup_path)
    if index == 0:
        global pagecache
        os.system('echo ' + pagecache + ' >memory.pagecache_limit')
    elif index == 1:
        global memory
        os.system('echo ' + memory + ' >memory.max')
    elif index == 2:
        global io
        os.system('echo ' + io + ' >io.max')

    os.system('cd -')


# the main thread of dynamic control strategy
def run():
    # initialize the latest index
    bi = 0
    so = 0
    bo = 0
    while True:
        ret = get_index()
        fluctuation = cmp_threashold(ret, bi, so, bo)
        # if some index exceeds
        if fluctuation != -1:
            handle_exceed(fluctuation)

        # fetch index every 1 sec
        time.sleep(1)

if __name__ == '__main__':
    run()
    
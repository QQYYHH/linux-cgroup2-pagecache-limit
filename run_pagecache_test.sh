#!/bin/bash
cd /sys/fs/cgroup/
if [ ! -d "test" ]; then
    mkdir test
fi

echo '+memory +io' >cgroup.subtree_control

# 将当前shell pid 写入 cgroup.procs
# 之后 fork 的子进程会自动加入 procs
echo $$ >test/cgroup.procs

# 控制pagecache 上限 - 1G
echo 1073741824 >test/memory.pagecache_limit

# 控制io 上限制 为 2M/S
# echo '8:0 wbps=2097152' >test/io.max

# 控制io 为 50K/s
echo '8:0 wbps=51200 rbps=51200' >test/io.max

cd -
# ./test
# dd if=/dev/zero of=io_pagecache_test bs=1M count=500
taskset -c 4,5 ./gen_big_file_whlie.sh
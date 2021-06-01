# linux-cgroup2-pagecache-limit
Add a new mechanic to cgroup-v2 and implement the control of page-cache.
Our linux version is 4.15.0 and the directory **linux-source-4.15.0** is the corresponding source code tree.

## modified files
To implement the new pagecache-limit mechanic, I modified the following files.
- include/linux/memcontrol.h
- include/linux/page_count.h
- mm/filemap.c
- mm/page_count
- mm/memcontrol.c
- mm/vmscan.c
If you use other versions of linux, please replace these files.

## how to use
You can install the new kernel following the instructions below:
```bash
# click Kernel hacking -> Complie-time checks -> compile the kernel with debug info
# generate drarf4 debuginfo
# provide GDB scripts for kernel debugging
make menuconfig
make -j8
make install 
make modules_install
```
Then you can boot the new kernel through GRUB. Before you boot the new kernel, you shold firstly disable the cgroup-v1 to allow cgroup-v2 using all sub-system:
```bash
vim /etc/default/grub
# append systemd.unified_cgroup_hierarchy=1 to GRUB_CMDLINE_LINUX
update-grup
```
After you boot the new kernel:
```bash
cd /sys/fs/cgroup/unified
echo '+memory' >cgroup.subtree_control
mkdir test & cd test & ls
```
You will see two new user interface files which is using to catch the usage of pagecache and the limitation of pagecache and you can use them like other user interface files. 
- `memory.pagecache_current`
- `memory.pagecache_limit`

## test
Run the scripts **run_pagecache_test.sh**. The script use dd to copy a file of 500M, and it limit the pagecache to 100M. After copying, you can use `cat memory.pagecache_current` to see how many bytes the dd process used. And you can also use `free -h` to see increments in buffer/cache usage.
At the same time, we provide a simple program to test, **test.c**, you can replace dd cmd with `./test` to do more tests. 



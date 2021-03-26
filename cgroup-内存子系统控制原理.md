# cgroup实战-基于cgroup-v2的新机制

## 简介

基于**cgroup内存子系统[memcontroller]** 添加用于限制**pagecache**的新机制。

最终效果：如果使用cgroup内存子系统，面向用户的接口文件会多出来两个：**memory.pagecache_limit** & **memory.pagecache_current** 分别用于限制pagecache最大使用量、展示当前pagecache使用量。使用方法和其他用户接口一样。

修改的内核源文件：

- **mm/filemap.c**

  主要修改涉及将page加入pagecache和LRU链表的部分。

- **include/linux/memcontrol.h**

  在mem_cgroup结构体内添加新的资源限制描述符(page_counter结构体)。

- **mm/memcontrol.c**

  初始化pagecache资源限制描述符（page_counter指针）。

  修改cgroup调控资源的核心逻辑，增加对pagecache的资源统计（分配+释放），在pagecache资源不足的情况下，触发对pagecache的页面回收机制。

- **mm/vmscan.c**

  页面回收相关。页面回收的大概逻辑如下：

  - 根据当前内存资源使用量 + 所需回收资源量，确定4个LRU链表内需要扫描的页数（非活动匿名页LRU、活动匿名页LRU、非活动文件页LRU、活动文件页LRU）
  - 将每个LRU中扫描到的页单独拿出来，尽可能回收，并统计已回收的页数
  - 判断已回收的页数 >= 所需回收资源量，如果不满足，跳转到step 1继续执行



## 内存子系统控制原理

cgroup管理内存的重点：**资源统计** + **资源回收**

拿pagecache作为例子，**资源统计**的时机在将page加入pagecache的时候【增加pagecache使用量】、page成功回收【减少】。

**资源回收**的时机在pagecache使用量超过阈值的时候。

下面以**读文件**作为例子，详细分析cgroup管理内存的原理。

### read系统调用主要逻辑

read-cgroup.png![image](https://user-images.githubusercontent.com/49839614/112600370-14a5c900-8e4c-11eb-9cfe-50ab41f98d75.png)


可以看出，将新分配的page加入到pagecache和非活动文件LRU队列的时候启动cgroup调控。

### add_to_page_cache_lru

```c
/* mm/filemap.c 
* 下面函数只突出主要逻辑
* @page: 待加入pagecache的page
* @mapping: 描述pagecache的address_space结构体
* @offset: page在文件中的偏移量，也就是radix_tree的index
*/
static int __add_to_page_cache_lru(struct page *page, struct address_space *mapping, pgoff_t offset, 
				gfp_t gfp_mask, void **shadowp){
  // 判断该page是否可以成功分配（资源统计 + 回收）
  int error = mem_cgroup_try_charge(page, current->mm, gfp_mask, &memcg, false); 
  if(error)
    return error;
  
  // 可以分配则加入 pagecache
  error = page_cache_tree_insert(mapping, page, shadowp);
  
  if(error)
    // 如果出错则 取消charge
    mem_cgroup_cancel_charge(page, memcg, false);
  
  // 并加入lru队列
  lru_cache_add(page);
  
  /* 最后提交本次资源charge */
  mem_cgroup_commit_charge(page, memcg, false); 
}

void mem_cgroup_commit_charge(struct page* page, struct mem_cgroup *memcg, bool lrucare){
  // page->mem_cgroup = memcg;
  // 将page和对应的memcg绑定
  commit_charge(page, memcg, lrucare);
  /**
  * 根据 page的类型（匿名页、文件页、swap-cache页）
  * 增加当前CPU缓存的 mem_cgroup_stat_cpu结构体 相应的资源数
  * 增加 一些事件数量（page-in, page-out）
  * memcg->stat就是指向 上述结构体的指针
  * __this_cpu_add(memcg->stat->count[MEMCG_RSS], nr_pages); 如果是匿名页
  * __this_cpu_add(memcg->stat->count[MEMCG_CACHE], nr_pages); 如果是文件页
  * __this_cpu_add(memcg->stat->count[MEMCG_SHMEM], nr_pages); 如果是属于swap-cache
  * __this_cpu_inc(memcg->stat->events[PGPGIN]); 如果nr_pages > 0，增加page-in事件数量
  * __this_cpu_inc(memcg->stat->events[PGPGOUT]); 如果nr_pages < 0，增加page-out事件数量
  */
  mem_cgroup_charge_statistics(memcg, page, copoud, nr_pages);
  
  /**
  * 检查一些事件，这些事件可能是用户添加，也可能系统自动添加
  * 如果事件条件满足则出发相应的 事件handler
  * 事件：比如说内存超过了用户规定的 阈值
  */
  memcg_check_events(memcg, page);
}
```



### try_charge

**mem_cgroup_try_charge 的核心函数是 try_charge**，同时**try_charge**也是cgroup内存资源调控的核心逻辑框架。

```c
/* mm/memcontrol.c */
static int try_charge(struct mem_cgroup *memcg, gfp_t gfp_mask, unsigned int nr_pages){
  /* 每次至少 charge batch 个页 (batch = 32) */
  unsigned int batch = max(nr_pages, CHARGE_BATCH);
  struct page_counter *counter; // 超过限制的memcg对应的资源计数
  struct mem_cgroup *mem_over_limit; // 超过限制的memcg
  int nr_retries = 5; // 最大重新尝试次数

retry:
  /**
  * 尝试消耗当前CPU缓存的 多余页面数量
  * 因为每次至少统计 batch个页
  * 当batch > nr_pages, 就会多出来 batch - nr_pages个页
  * 如果可以，直接返回
  */
  if(consume_stock(memcg, nr_pages))
    return 0;
  
  /**
  * 尝试 分配所需页面数，从当前cgroup开始，向上一直统计到根结点
  * 因为每个cgroup节点统计的是 整颗子树的资源使用量
  * 因此当前 节点增加资源统计，父节点也要增加相应的资源统计
  * counter是第一个不满足 限制的 节点
  */
  int success = page_counter_try_charge(&memcg->memory, batch, &counter);
  if(success) goto done_restock; // 如果成功，补充当前CPU缓存的页面数量
  
  /* 如果不成功 */
  if(batch > nr_pages){ // 调小 batch，再试一次
		batch = nr_pages;
    goto retry;
  }
  
  /**
  * 分配不能失败
  * 可能不久就会释放大量空闲页面
  * 就强行分配
  */
  if(满足强行分配条件) 
    goto force; 
  
  /* 接下来就尝试释放nr_pages个空闲页，然后再重新charge */
  
  // 获取超过限制的cgroup节点
  mem_over_limit = mem_cgroup_from_counter(counter, memory); 
  int nr_reclaimed = try_to_free_mem_cgroup_pages(mem_over_limit, nr_pages, gfp_mask, may_swap);
  
  // 释放之后，如果 counter->limit - counter->count >= nr_pages
  // 也就是说分配这么多页 不会超过限制，就重新尝试 charge
  if(mem_cgroup_margin(mem_over_limit) >= nr_pages)
    goto retry;
  
  if(第一次执行此判断){
    /**
    * 释放所有CPU缓存 的多出来的页面
    * 最终 这些多出来的页面 从page_counter->count中减掉
    * 然后重新尝试
    */
    drain_all_stock(mem_over_limit);
    goto retry;
  }
  
  // 如果释放的有空闲页，且所需求的nr_pages不是太多则再次尝试
	if(nr_reclaimed && nr_pages <= 8)
    goto retry;
  
  // 这里控制 在一直没有页面释放的情况下最多尝试5次
  if(nr_retries--)
    goto retry;
  
  // 最终还是不行就报内存不足错误
  return -ENOMEM;
  
done_restock:
  // 补充 CPU 缓存的页面数
  if(batch > nr_pages)
    refill_stock(memcg, batch - nr_pages);
  
force:
  // 强制分配，不检测是否超过limit
  page_counter_charge(&memcg->memory, nr_pages);
  
  return 0;
}


/**
* 层级向上统计counter指定的资源【内存、pagecache等】 counter->count += nr_pages
* @counter 要统计的起点
* @nr_pages 需要增加的页数
* @fail 第一个超过限制的counter
*/
bool page_counter_try_charge(struct page_counter *counter, unsigned long nr_pages, 
						struct page_counter **fail){
  struct page_counter *c;
  for(c = counter; c; c = c->parent;){ // 一直向上遍历到根结点
    // 原子操作 c->count += nr_pages
    long new = atomic_long_add_return(nr_pages, &c->count);
    if(new > c->limit){ // 如果超过限制
			atomic_long_sub(nr_pages, &c->count); // 原子剪掉
      
      *fail = c; // 记录第一个超过限制的 counter
      goto failed;
    }
  }
  return true;
failed:
  for(c = counter; c != *fail; c = c->parent){ // 撤销前面的操作
    // atomic_long_sub(nr_pages, c->count);
    // 原子剪掉
    page_counter_cancel(c, nr_pages);
  }
  return false;
}
```



### try_to_free_mem_cgroup_pages

此调用负责内核页面回收

内核回收机制触发有两种方式：直接调用（direct reclaim）、回收线程（kswapd）

这里只说 direct reclaim

#### 整体回收调用链

```c
vmscan.c:
	try_to_free_mem_cgroup_pages // cgroup 回收内存页
		do_try_to_free_pages // 回收内存页，控制优先级，优先级决定一次扫描的页数
    	shrink_zones // 循环遍历每一个 zone 区
    	|if(global(sc)) // 执行下面
    	|	mem_cgourp_soft_limit_reclaim
    	|		mem_cgroup_soft_reclaim
    	|			mem_cgroup_shrink_node
    	|				shrink_node_memcg
      |  
    	|else // cgroup控制的内存回收会执行这个分支
      | shrink_node // 最外层循环控制是否停止回收，内层循环遍历cgroup子树，回收每个子节点页面
    	|		shrink_node_memcg
    	|			shrink_list
      |  			shrink_active_list // 将某些page从active_list 移到 inactive_list
      |				shrink_inactive_list
    	|					shrink_page_list // 真正执行页面回收，前面都是做一些准备、控制
    	|						pageout -> mapping->a_ops->writepage // 如果回收的页面是脏页，就先等其pageout再回收
    	|		shrink_slab // 回收 inode 或者 dentry cache
```



#### 核心函数详解

```c
/* mm/vmscan.c */
unsigned long try_to_free_mem_cgroup_pages(struct mem_cgroup *memcg,
					   unsigned long nr_pages,
					   gfp_t gfp_mask,
					   bool may_swap)
{
  // 控制页面回收的结构体
  struct scan_control sc = {
		.nr_to_reclaim = max(nr_pages, SWAP_CLUSTER_MAX), // 要回收的页面数量
		.gfp_mask = (current_gfp_context(gfp_mask) & GFP_RECLAIM_MASK) |
				(GFP_HIGHUSER_MOVABLE & ~GFP_RECLAIM_MASK),
		.reclaim_idx = MAX_NR_ZONES - 1, // 要扫描的zone区（normal区，highmem区，DMA区）等，这里是全部扫描
		.target_mem_cgroup = memcg, // 管控的cgroup
    /* nr_scan = 要扫描的LRU链表长度 >> sc->priority */
		.priority = DEF_PRIORITY, // 优先级，值越小，优先级越高，一次性扫描的页面数量越多
		.may_writepage = !laptop_mode,
		.may_unmap = 1,
		.may_swap = may_swap,
	};
  
  struct zonelist *zonelist = &NODE_DATA(nid)->node_zonelists[ZONELIST_FALLBACK]; // 获取当前node的zone列表
  
  unsigned long nr_reclaimed = do_try_to_free_pages(zonelist, &sc);
  
}

/**
* direct reclaim的主要入口函数
*/
static unsigned long do_try_to_free_pages(struct zonelist *zonelist,
					  struct scan_control *sc)
{
  /**
	 * 假设 LRU 链表长度为 size
	 * 那么每次只扫描(scan) size >> sc->priority 个页
	 * 如果没有满足 回收页面数量要求，会首先递减优先级（优先级数值越小，每次扫描的页面数量越多）
	 * 然后开始新一轮的释放，直到满足要求
	 */
  do{
    shrink_zone(zone_list, sc);
    
    // 满足条件，直接返回
    if(sc->nr_reclaimed >= sc->nr_to_reclaim)
      return nr_reclaimed;
    
  }while(--sc->priority >= 0);
  
  return 0;
    
}


/**
* 循环扫描每一个zone区（normal, highmem, DMA）
*/
static void shrink_zones(struct zonelist *zonelist, struct scan_control *sc)
{
  for(zone in zonelist){
    // 获取每一个zone所属的 node
    // pg_data_t 描述 node 的内存布局
    struct pg_data_t *pgdat = zone->zone_pgdat;
    shrink_node(pgdat, sc);
  }
}


/**
* 核心逻辑为 二层循环
* 外层控制 是否停止回收
* 内层遍历cgroup子树，回收每个子节点管理的页面
*/
static bool shrink_node(pg_data_t *pgdat, struct scan_control *sc)
{
	do{ // 外层控制是否继续回收
    struct mem_cgroup_reclaim_cookie reclaim = {
      .pgdat = pgdat, 
      .priority = sc->priority, 
    };
    // 获取cgroup根节点
    struct mem_cgroup *memcg, *root = sc->target_mem_cgroup; 
    memcg = mem_cgroup_iter(root, NULL, &reclaim);
    do{ // 内层循环遍历cgroup子树
      
      // 如果当前memcg内存使用量过低，被保护，不回收
      if(mem_cgroup_low(root, memcg)) continue;
      
      shrink_node_memcg(pgdat, memcg, sc, &reclaimed_pages); // 回收关键
      
      if(memcg)
        shrink_slab(); // 回收 kmem (inode缓存或dentry缓存)
      
      if(sc->nr_reclaimed >= sc->nr_to_reclaim){ // 满足需求，break
        mem_cgroup_iter_break(root, memcg);
        break;
      }
      
    }while((memcg = mem_cgroup_iter()));
    
  }while(should_continue_reclaim(root, memcg, &reclaim));
  
  return true;
}


/**
* basic per-node page freer. Used by both kswapd and direct reclaim.
* @pgdat: 指定node内存布局
* @lru_pages: 该函数一共回收多少page
*/
static void shrink_node_memcg(struct pglist_data *pgdat, struct mem_cgroup *memcg,
			      struct scan_control *sc, unsigned long *lru_pages)
{
  /**
	 * 获得memcg管理的 4个lru链表
	 * lruvec -> lists[NR_LRU_LISTS]
	 */
	struct lruvec *lruvec = mem_cgroup_lruvec(pgdat, memcg);
  unsigned long nr[NR_LRU_LISTS];
  unsigned long nr_reclaimed = 0;
  
  /**
	 * 计算 LRU_INACTIVE_ANON LRU_ACTIVE_ANON LRU_INACTIVE_FILE LRU_ACTIVE_FILE
	 * 这4个 LRU链表中需要 scan的页数，分别记录在 nr[0] - nr[4]
	 * lru_pages 是 所有LRU链表中的 总页数
	 */
	get_scan_count(lruvec, memcg, sc, nr, lru_pages);
  
  // 如果 有一个链表还需要扫描就继续循环
  while (nr[LRU_INACTIVE_ANON] || nr[LRU_ACTIVE_FILE] ||
					nr[LRU_INACTIVE_FILE] || nr[LRU_ACTIVE_ANON]){ 
    
    for_each_lru(lru){ // 循环遍历每个LRU链表
			if(nr[lru]){ // 该链表仍需要扫描
        int nr_to_scan = min(nr[lru], 32); // 一次最多扫32个
        nr[lru] -= nr_to_scan;
        
        nr_reclaimed += shrink_list(lru, nr_to_scan, lruvec, sc); // 重点
        
      }
    }
    
  }
  // 更新控制结构体中的 回收页面数量
  sc->nr_reclaimed += nr_reclaimed;
}


static unsigned long shrink_list(enum lru_list lru, unsigned long nr_to_scan,
				 struct lruvec *lruvec, struct scan_control *sc)
{
	/**
	 * lru类型是否是active_list，如果是，将该LRU链表中某些page转移到inactive_list
	 */
	if (is_active_lru(lru)) {
		if (inactive_list_is_low(lruvec, is_file_lru(lru), sc, true))
			shrink_active_list(nr_to_scan, lruvec, sc, lru);
		return 0;
	}

	return shrink_inactive_list(nr_to_scan, lruvec, sc, lru);
}

// 真实回收页面的过程
// ret: 回收的页数
static noinline_for_stack unsigned long
shrink_inactive_list(unsigned long nr_to_scan, struct lruvec *lruvec,
		     struct scan_control *sc, enum lru_list lru)
{
  // 申请待回收的页面链表
  LIST_HEAD(page_list);
  
  /* 将 pagevec 缓冲区中的页 加入 active_list or inactive_list
	* lru_cache_add 会将 page 首先加入 pagevec，等到积攒一定数量的页面 由某线程统一加入 lru链表
	*/
  lru_add_drain();
  
  spin_lock_irq(&pgdat->lru_lock);
  /**
	 * 将 lruvec -> list[lru] 中，也就是某个LRU链表中的页面转移到 page_list
	 * 调用该函数前 要获取适当的锁
	 */
	unsigned long nr_taken = isolate_lru_pages(nr_to_scan, lruvec, &page_list,
				     &nr_scanned, sc, isolate_mode, lru);
  
  spin_unlock_irq(&pgdat->lru_lock);
  
  if(nr_taken == 0) return 0;
  
  // 最终页面回收的关键函数
  unsigned long nr_reclaimed = shrink_page_list(&page_list, pgdat, sc, 0, &stat, false); 
  
  /**
	 * 经过 shrink_page_list后，page_list中剩下的页面就是没有被回收的
	 * 将page_list中 的页面放入lruvec 合适的 LRU链表中
	 * 并再次检查每个页面是否可回收，如果可以，立刻回收, 并重新加入page_list
	 */
	putback_inactive_pages(lruvec, &page_list);
  
  
  mem_cgroup_uncharge_list(&page_list);
	/* 向操作系统提交回收的页面 */
	free_unref_page_list(&page_list);
  return nr_reclaimed;
}

// 该函数逻辑略显复杂，详细的代码和注释在源码里
static unsigned long shrink_page_list(struct list_head *page_list,
				      struct pglist_data *pgdat,
				      struct scan_control *sc,
				      enum ttu_flags ttu_flags,
				      struct reclaim_stat *stat,
				      bool force_reclaim)
{
  LIST_HEAD(ret_pages);
  LIST_HEAD(free_pages);
  unsigned long nr_reclaimed = 0;
  // 循环尝试释放局部链表中 所有的页
  // 将成功释放的页加入 free_pages链表
  // 未成功 的加入 ret_pages链表
  while(!list_empty(page_list)){
    
    1. 页面被锁住，继续放入 inactive_list中 ;
    
    2./*
		 * 一个页正在写回，会有3种情况
		 * 1. 有大量page等待被写回，此时，如果等待可能会等很久。因此继续留在inactive_list，等待下一次处理
		 * 2. 如果page没被标记为 PageReclaim，将其标记，并留在 inactive_list，等待下一次处理
		 * 3. 如果一个page 被标记且此时没有太多页面等待写回，就阻塞等待该page完成写回操作，然后重新尝试该page是否可以回收
		 */;
    
    3. 检查 page 是否再次被访问过，如果是，留在inactive链表中，继续扫描下一个页;
    
    4. /*
		 * 如果是 匿名页
		 * 如果该 page 不在 swap-cache，为其分配 swap_enrty_t 并将其加入 swap-cache
		 */;
    
    5. /*
		 * 如果存在 页面相关的pte，尝试 unmap，这里用到了RMAP算法 
		 */;
    if(page_mapped(page))
      try_to_unmap(page, flags);
    try_to_unmap_flush_dirty();
    
    6. /*
		 * 如果页面是 脏页，调用pageout写回到后备文件中
		 */;
    /**
			 * PAGE_KEEP: 页面继续留在 inactive_list
			 * PAGE_ACTIVE: 页面可能再次被引用，加入 active_list
			 * PAGE_SUCCESS: 写回完成，再次检查是否是脏页
			 */
			switch (pageout(page, mapping, sc)) {
			case PAGE_KEEP:
				goto keep_locked;
			case PAGE_ACTIVATE:
				goto activate_locked;
			case PAGE_SUCCESS:
				if (PageWriteback(page))
					goto keep;
				if (PageDirty(page))
					goto keep;

				/*
				 * A synchronous write - probably a ramdisk.  Go
				 * ahead and try to reclaim the page.
				 */
				if (!trylock_page(page))
					goto keep;
				if (PageDirty(page) || PageWriteback(page))
					goto keep_locked;
				mapping = page_mapping(page);
			case PAGE_CLEAN:
				; /* try to free the page below */
			}
    
    7. /**
		 * 如果页面和 buffers 关联，将 buffers & buffer_head释放
		 * 调用 try_to_release_page
		 * 如果页面和 buffers关联，struct buffer_head会保存在 page->private字段
		 * 且 page->PG_private会被 置位
		 */;
    if(page_has_private(page)){
      try_to_release_page(page);
    }
    
    // 如果成功释放
    nr_reclaimed ++;
    list_add(&page_lru, &free_pages);
    
    // 如果没成功
    list_add(&page_lru, &ret_pages);
    
  }
  
  /**
	* 还原 mem_cgroup 中 page_counter的资源统计量
	*/
	mem_cgroup_uncharge_list(&free_pages);
	try_to_unmap_flush();
	/* 向操作系统提交回收的页面 */
	free_unref_page_list(&free_pages);
  
  // 将剩下没被回收的页面送回 原始page_list链表
  list_splice(&ret_pages, page_list);
  
  return nr_reclaimed;
}


```





### cgroup资源计数

在**try_charge**中通过 **page_counter_try_charge**增加资源使用量。并且将page和对应的cgroup绑定

**try_charge**成功之后，如果后续因为其他原因报错（page加入pagecache失败或加入LRU链表失败），需要取消资源统计，还原，调用**mem_cgroup_cancel_charge**，最终调用**page_counter_uncharge**

在触发页面回收机制，释放完页面后，调用**mem_cgroup_uncharge_list**，释放所有页面占用的资源量，最终调用的是**page_counter_uncharge**，并且解除page和对应cgroup的绑定



### LRU相关

```c
/*
* 每个node维护完整的LRU链表
* 每个zone也会有自己局部的 LRU 链表
* memcg 管理一个node里面的页面，这些页面可能分布在不同的zone
* 相应的结构体(struct mem_cgroup_per_node) 记录了memcg管理的LRU链表
* 同时记录了 不同zone区，不同类型 的LRU链表内的页数
* 也就是说struct page* 会在3种LRU链表中
* 1 - node_lru
* 2 - zone_lru
* 3 - memcg_lru
*/

// page 是否 是 file-backed page
page_is_file_cache(page);
// page是否在 file_lru中
is_file_lru(page);


// 获取page->mem_cgroup 管理的 LRU链表
mem_cgroup_page_lruvec(page, pgdat)
/**
 * lruvec_lru_size -  Returns the number of pages on the given LRU list.
 * @lruvec: lru vector
 * @lru: lru to use
 * @zone_idx: zones to consider (use MAX_NR_ZONES for the whole LRU list)
 */
lruvec_lru_size()
  
// 将page add 到相应lru链表
lru_cache_add(page);


```



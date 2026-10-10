package cache

import "example.com/shop/v2/internal/store"

type Cache struct {
	s    *store.Store
	hits int
}

func New(s *store.Store) *Cache {
	return &Cache{s: s}
}

func (c *Cache) Get(key string) int {
	v, ok := c.s.Get(key)
	if !ok {
		return 0
	}
	c.hits++
	return v
}

func (c *Cache) Warm(keys []string) {
	for _, k := range keys {
		c.s.Put(k, 0)
	}
	evict(c)
}

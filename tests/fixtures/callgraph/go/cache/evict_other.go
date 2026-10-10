//go:build !linux

package cache

func evict(c *Cache) {
	c.hits = -1
}

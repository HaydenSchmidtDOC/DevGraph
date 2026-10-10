package cache_test

import (
	"testing"

	"example.com/shop/v2/cache"
	"example.com/shop/v2/internal/store"
)

// This is package cache_test: a bare New() means this helper, and the
// package under test is only reachable qualified, as cache.New.
func New() *cache.Cache {
	return cache.New(store.New())
}

func TestWarm(t *testing.T) {
	c := New()
	c.Warm([]string{"x"})
	if c.Get("x") != 0 {
		t.Errorf("want 0")
	}
}

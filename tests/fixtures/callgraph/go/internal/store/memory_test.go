package store

import "testing"

func TestGet(t *testing.T) {
	s := New()
	s.Put(" A ", 1)
	if v, _ := s.Get("a"); v != 1 {
		t.Fatal("want 1")
	}
}

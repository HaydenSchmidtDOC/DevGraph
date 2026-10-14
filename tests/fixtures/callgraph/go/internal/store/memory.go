package store

type Store struct {
	items map[string]int
}

func New() *Store {
	return &Store{items: map[string]int{}}
}

func (s *Store) Get(key string) (int, bool) {
	v, ok := s.items[normalize(key)]
	return v, ok
}

func (s *Store) Put(key string, v int) {
	s.items[normalize(key)] = v
}

package db

type Conn struct {
	dsn string
}

func Open(dsn string) *Conn {
	return &Conn{dsn: dsn}
}

func (c *Conn) Close() error {
	return nil
}

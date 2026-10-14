package store

import "strings"

func normalize(k string) string {
	return strings.ToLower(strings.TrimSpace(k))
}

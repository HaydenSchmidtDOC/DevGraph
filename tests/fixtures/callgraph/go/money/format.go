package money

import "fmt"

func Format(cents int) string {
	return fmt.Sprintf("$%d.%02d", whole(cents), frac(cents))
}

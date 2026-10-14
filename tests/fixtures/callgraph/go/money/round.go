package money

func whole(c int) int {
	return c / 100
}

func frac(c int) int {
	return abs(c % 100)
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

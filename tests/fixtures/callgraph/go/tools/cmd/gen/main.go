package main

import (
	"os"

	"example.com/shop/tools/internal/codegen"
	"example.com/shop/v2/money"
)

func main() {
	out := codegen.Render(money.Format(1999))
	writeFile(out)
}

func writeFile(s string) {
	os.WriteFile("prices_gen.txt", []byte(s), 0o644)
}

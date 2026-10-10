package main

import (
	"fmt"
	"net/http"

	"example.com/shop/v2/cache"
	"example.com/shop/v2/internal/store"
	"example.com/shop/v2/money"
	"github.com/go-chi/chi/v5"
)

func main() {
	s := store.New()
	c := cache.New(s)
	fmt.Println(money.Format(c.Get("sku-1")))
	run(c)
	migrate()
	http.ListenAndServe(":8080", routes())
}

func run(c *cache.Cache) {
	c.Warm([]string{"sku-1", "sku-2"})
}

func routes() http.Handler {
	r := chi.NewRouter()
	r.Get("/healthz", health)
	return r
}

func health(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
}

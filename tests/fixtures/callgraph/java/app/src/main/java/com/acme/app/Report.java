package com.acme.app;

final class Report {
    private Report() {}

    static void print(OrderService service) {
        System.out.printf("%d pages%n", service.pages());
    }
}

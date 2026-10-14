package com.acme.app;

import com.acme.core.config.Settings.Defaults;
import com.acme.core.notify.EmailNotifier;

public class App {
    public static void main(String[] args) {
        OrderService service = new OrderService(new EmailNotifier());
        service.place("Blue Widget");
        System.out.println(service.pages() + " / " + Defaults.pageSize());
        Report.print(service);
    }
}

package com.acme.core.notify;

public class EmailNotifier implements Notifier {
    @Override
    public void send(String message) {
        System.out.println("email: " + message);
    }
}

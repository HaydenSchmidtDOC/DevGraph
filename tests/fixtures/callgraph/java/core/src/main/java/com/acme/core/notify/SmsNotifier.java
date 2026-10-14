package com.acme.core.notify;

public class SmsNotifier implements Notifier {
    @Override
    public void send(String message) {
        System.out.println("sms: " + message);
    }
}

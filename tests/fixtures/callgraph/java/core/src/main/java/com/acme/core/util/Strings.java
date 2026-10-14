package com.acme.core.util;

public final class Strings {
    private Strings() {}

    public static String slug(String s) {
        return s.trim().toLowerCase().replace(' ', '-');
    }
}

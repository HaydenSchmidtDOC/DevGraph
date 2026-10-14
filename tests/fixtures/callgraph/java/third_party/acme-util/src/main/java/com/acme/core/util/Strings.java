package com.acme.core.util;

// Vendored older copy of com.acme.core.util.Strings (same FQCN). Not built.
public final class Strings {
    private Strings() {}

    public static String slug(String s) {
        return collapse(s.strip());
    }

    static String collapse(String s) {
        return s.replaceAll("\\s+", "_");
    }
}

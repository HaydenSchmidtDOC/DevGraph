package com.acme.core.repo;

import com.acme.core.model.Item;
import java.util.ArrayList;
import java.util.List;

public class Repo {
    protected final List<Item> items = new ArrayList<>();

    public void add(Item item) {
        validate(item);
        items.add(item);
    }

    public int size() {
        return items.size();
    }

    protected void validate(Item item) {
        if (item.getName().isEmpty()) {
            throw new IllegalArgumentException("name");
        }
    }
}

package com.acme.core.repo;

import com.acme.core.model.Item;
import com.acme.core.util.*;
import java.util.List;

// Same package as Repo, so Repo needs no import.
public class AuditedRepo extends Repo {
    @Override
    public void add(Item item) {
        super.add(item);
        Log.info("added " + item.getName());
    }

    public void addAll(List<Item> batch) {
        for (Item i : batch) {
            add(i);
        }
    }

    public void check(Item item) {
        validate(item);
    }
}

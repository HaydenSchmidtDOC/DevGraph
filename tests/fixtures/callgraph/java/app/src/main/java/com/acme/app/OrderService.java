package com.acme.app;

import static com.acme.core.config.Settings.Defaults.pageSize;

import com.acme.core.model.Item;
import com.acme.core.notify.Notifier;
import com.acme.core.repo.AuditedRepo;
import com.acme.core.util.*;
import java.util.ArrayList;
import java.util.List;

public class OrderService {
    private final AuditedRepo repo = new AuditedRepo();
    private final Notifier notifier;

    public OrderService(Notifier notifier) {
        this.notifier = notifier;
    }

    public void place(String name) {
        Item item = new Item(Strings.slug(name));
        repo.add(item);
        notifier.send("placed " + item.getName());
    }

    public int pages() {
        return (repo.size() + pageSize() - 1) / pageSize();
    }

    public List<String> names(List<Item> items) {
        List<String> out = new ArrayList<>();
        for (Item i : items) {
            out.add(i.getName());
        }
        return out;
    }
}

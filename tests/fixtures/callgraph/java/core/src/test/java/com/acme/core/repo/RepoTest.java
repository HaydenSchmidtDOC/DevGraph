package com.acme.core.repo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.acme.core.model.Item;
import org.junit.jupiter.api.Test;

class RepoTest {
    @Test
    void addsItem() {
        Repo repo = new Repo();
        repo.add(new Item("bolt"));
        assertEquals(1, repo.size());
    }
}

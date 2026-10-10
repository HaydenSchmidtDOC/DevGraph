package com.acme.notes.util

import kotlin.test.Test
import kotlin.test.assertEquals

class TextTest {
    @Test
    fun slugifies() {
        assertEquals("a-b", slugify("A B"))
    }
}

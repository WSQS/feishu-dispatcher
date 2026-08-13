@file:OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)

package dev.sopho.fdx.client.tree

import dev.sopho.fdx.client.network.TreeChildrenEntry
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TreeLoaderTest {
    private fun dir(name: String, path: String) = TreeChildrenEntry(name, path, "directory")

    private fun file(name: String, path: String) = TreeChildrenEntry(name, path, "file")

    @Test
    fun `start_requests_only_root_and_applies_children`() = runTest {
        val requested = mutableListOf<String>()
        val loader = TreeLoader(this) { path ->
            requested += path
            listOf(dir("a", "a"), file("b.txt", "b.txt"))
        }

        loader.start()
        runCurrent()

        assertEquals(listOf(""), requested)
        assertEquals(listOf("a", "b.txt"), loader.state.value.visibleRows().map { it.path })
        assertFalse(loader.state.value.directories.getValue(ROOT_PATH).loading)
    }

    @Test
    fun `start_twice_requests_root_once`() = runTest {
        val requested = mutableListOf<String>()
        val loader = TreeLoader(this) { path ->
            requested += path
            emptyList()
        }

        loader.start()
        loader.start()
        runCurrent()

        assertEquals(listOf(""), requested)
    }

    @Test
    fun `expand_cold_directory_shows_loading_then_applies_children`() = runTest {
        val gate = CompletableDeferred<Unit>()
        val requested = mutableListOf<String>()
        val loader = TreeLoader(this) { path ->
            requested += path
            if (path == "a") {
                gate.await()
                listOf(file("a/x.txt", "a/x.txt"), file("a/y.txt", "a/y.txt"))
            } else {
                listOf(dir("a", "a"))
            }
        }

        loader.start()
        runCurrent()
        loader.toggle("a")
        runCurrent()

        val loading = loader.state.value.visibleRows().first { it.path == "a" }
        assertTrue(loading.loading)
        assertNull(loading.error)
        assertTrue("a" in loader.state.value.expandedPaths)

        gate.complete(Unit)
        runCurrent()

        assertEquals(listOf("", "a"), requested)
        assertEquals(
            listOf("a", "a/x.txt", "a/y.txt"),
            loader.state.value.visibleRows().map { it.path },
        )
    }

    @Test
    fun `collapse_cancels_inflight_load_and_discards_response`() = runTest {
        val gate = CompletableDeferred<Unit>()
        val loader = TreeLoader(this) { path ->
            if (path == "a") {
                gate.await()
                listOf(file("a/x.txt", "a/x.txt"))
            } else {
                listOf(dir("a", "a"))
            }
        }

        loader.start()
        runCurrent()
        loader.toggle("a")
        runCurrent()
        loader.toggle("a")
        runCurrent()
        assertFalse("a" in loader.state.value.expandedPaths)

        gate.complete(Unit)
        runCurrent()

        val rows = loader.state.value.visibleRows()
        assertTrue(rows.none { it.path.startsWith("a/") })
    }

    @Test
    fun `collapse_then_reexpand_ignores_stale_response`() = runTest {
        val oldGate = CompletableDeferred<Unit>()
        val newGate = CompletableDeferred<Unit>()
        var aRequests = 0
        val loader = TreeLoader(this) { path ->
            if (path == "a") {
                aRequests++
                if (aRequests == 1) {
                    withContext(NonCancellable) { oldGate.await() }
                    listOf(file("a/stale.txt", "a/stale.txt"))
                } else {
                    newGate.await()
                    listOf(file("a/fresh.txt", "a/fresh.txt"))
                }
            } else {
                listOf(dir("a", "a"))
            }
        }

        loader.start()
        runCurrent()
        loader.toggle("a")
        runCurrent()
        loader.toggle("a")
        runCurrent()
        loader.toggle("a")
        runCurrent()

        newGate.complete(Unit)
        runCurrent()
        oldGate.complete(Unit)
        runCurrent()

        assertEquals(2, aRequests)
        val entries = loader.state.value.directories.getValue("a").entries.map { it.path }
        assertTrue("a/fresh.txt" in entries)
        assertFalse("a/stale.txt" in entries)
    }

    @Test
    fun `loaded_directory_reexpand_does_not_reload`() = runTest {
        var requests = 0
        val loader = TreeLoader(this) { path ->
            requests++
            if (path == "a") listOf(file("a/x.txt", "a/x.txt")) else listOf(dir("a", "a"))
        }

        loader.start()
        runCurrent()
        loader.toggle("a")
        runCurrent()
        loader.toggle("a")
        runCurrent()
        loader.toggle("a")
        runCurrent()

        assertEquals(2, requests)
        assertEquals(listOf("a", "a/x.txt"), loader.state.value.visibleRows().map { it.path })
    }

    @Test
    fun `load_failure_sets_error_and_keeps_other_children`() = runTest {
        val loader = TreeLoader(this) { path ->
            if (path == "a") {
                throw RuntimeException("boom")
            } else {
                listOf(dir("a", "a"), file("b.txt", "b.txt"))
            }
        }

        loader.start()
        runCurrent()
        loader.toggle("a")
        runCurrent()

        val row = loader.state.value.visibleRows().first { it.path == "a" }
        assertEquals("boom", row.error)
        assertFalse(row.loading)
        assertTrue(loader.state.value.visibleRows().any { it.path == "b.txt" })
        assertTrue(loader.state.value.directories.getValue("a").entries.isEmpty())
    }

    @Test
    fun `close_cancels_inflight_and_discards_late_response`() = runTest {
        val gate = CompletableDeferred<Unit>()
        val loader = TreeLoader(this) { path ->
            if (path == ROOT_PATH) {
                withContext(NonCancellable) { gate.await() }
                listOf(file("late.txt", "late.txt"))
            } else {
                emptyList()
            }
        }

        loader.start()
        runCurrent()
        loader.close()
        gate.complete(Unit)
        runCurrent()

        val root = loader.state.value.directories.getValue(ROOT_PATH)
        assertTrue(root.entries.isEmpty())
        assertTrue(root.loading)
    }
}

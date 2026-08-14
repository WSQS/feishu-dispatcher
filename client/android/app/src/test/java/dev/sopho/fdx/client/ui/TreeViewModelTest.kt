package dev.sopho.fdx.client.ui

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TreeViewModelTest {
    private val jsonHeaders = headersOf("Content-Type", "application/json")
    private val rootJson =
        """{"path":"","entries":[{"name":"src","path":"src","type":"directory"},{"name":"README.md","path":"README.md","type":"file"}]}"""
    private val srcJson =
        """{"path":"src","entries":[{"name":"Main.kt","path":"src/Main.kt","type":"file"}]}"""

    @Test
    fun `creation requests only root children`() = runVmTest {
        val requested = mutableListOf<String>()
        val engine = MockEngine { request ->
            requested += request.url.parameters["path"] ?: "<missing>"
            assertEquals("/api/projects/proj/tree/children", request.url.encodedPath)
            respond(rootJson, HttpStatusCode.OK, jsonHeaders)
        }
        val client = mockClient(engine)

        try {
            val vm = TreeViewModel("proj", client)
            runUntilIdle()

            assertEquals(listOf(""), requested)
            assertEquals(listOf("src", "README.md"), vm.state.value.visibleRows().map { it.path })
            assertFalse(vm.state.value.directories.getValue("").loading)
        } finally {
            client.close()
        }
    }

    @Test
    fun `toggle loads only selected cold directory`() = runVmTest {
        val requested = mutableListOf<String>()
        val engine = MockEngine { request ->
            val path = request.url.parameters["path"] ?: "<missing>"
            requested += path
            respond(if (path.isEmpty()) rootJson else srcJson, HttpStatusCode.OK, jsonHeaders)
        }
        val client = mockClient(engine)

        try {
            val vm = TreeViewModel("proj", client)
            runUntilIdle()
            vm.toggle("src")
            runUntilIdle()

            assertEquals(listOf("", "src"), requested)
            assertEquals(
                listOf("src", "src/Main.kt", "README.md"),
                vm.state.value.visibleRows().map { it.path },
            )
            assertTrue("src" in vm.state.value.expandedPaths)
        } finally {
            client.close()
        }
    }

    @Test
    fun `retry reloads only failed directory and keeps other rows`() = runVmTest {
        val requested = mutableListOf<String>()
        var srcCalls = 0
        val engine = MockEngine { request ->
            val path = request.url.parameters["path"] ?: "<missing>"
            requested += path
            when {
                path.isEmpty() -> respond(rootJson, HttpStatusCode.OK, jsonHeaders)
                srcCalls++ == 0 -> respond("boom", HttpStatusCode.InternalServerError)
                else -> respond(srcJson, HttpStatusCode.OK, jsonHeaders)
            }
        }
        val client = mockClient(engine)

        try {
            val vm = TreeViewModel("proj", client)
            runUntilIdle()
            vm.toggle("src")
            runUntilIdle()

            assertNotNull(vm.state.value.directories.getValue("src").error)
            assertTrue(vm.state.value.visibleRows().any { it.path == "README.md" })

            vm.retry("src")
            assertTrue(vm.state.value.directories.getValue("src").loading)
            runUntilIdle()

            assertEquals(listOf("", "src", "src"), requested)
            assertNull(vm.state.value.directories.getValue("src").error)
            assertEquals(
                listOf("src", "src/Main.kt", "README.md"),
                vm.state.value.visibleRows().map { it.path },
            )
        } finally {
            client.close()
        }
    }
}

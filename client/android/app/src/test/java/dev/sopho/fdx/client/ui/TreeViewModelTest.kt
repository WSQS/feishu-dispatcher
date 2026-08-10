package dev.sopho.fdx.client.ui

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull

class TreeViewModelTest {
    private val okJson = """{"entries":[{"path":"a.txt","type":"file","size":10}]}"""
    private val jsonHeaders = headersOf("Content-Type", "application/json")

    @Test
    fun `start_twice_sends_only_one_request`() = runVmTest {
        var calls = 0
        val engine = MockEngine { _ ->
            calls++
            respond(okJson, HttpStatusCode.OK, jsonHeaders)
        }
        val vm = TreeViewModel("proj")
        val client = mockClient(engine)

        vm.start(client)
        runUntilIdle()
        vm.start(client) // started 守卫：第二次应跳过
        runUntilIdle()

        assertEquals(1, calls)
        assertNotNull(vm.entries)
        assertNull(vm.error)
    }

    @Test
    fun `start_success_sets_entries`() = runVmTest {
        val engine = MockEngine { respond(okJson, HttpStatusCode.OK, jsonHeaders) }
        val vm = TreeViewModel("proj")

        vm.start(mockClient(engine))
        runUntilIdle()

        assertEquals(1, vm.entries?.size)
        assertEquals("a.txt", vm.entries?.get(0)?.path)
        assertEquals(10L, vm.entries?.get(0)?.size)
        assertNull(vm.error)
    }

    @Test
    fun `start_failure_sets_error_keeps_data_null`() = runVmTest {
        val engine = MockEngine { respond("", HttpStatusCode.InternalServerError) }
        val vm = TreeViewModel("proj")

        vm.start(mockClient(engine))
        runUntilIdle()

        assertNull(vm.entries)
        assertNotNull(vm.error)
    }
}

package dev.sopho.fdx.client.ui

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull

class ProjectListViewModelTest {
    private val okJson = """{"items":[{"name":"proj-a","path":"/a","default_agent":"copilot"}]}"""
    private val jsonHeaders = headersOf("Content-Type", "application/json")

    @Test
    fun `start_twice_sends_only_one_request`() = runVmTest {
        var calls = 0
        val engine = MockEngine { _ ->
            calls++
            respond(okJson, HttpStatusCode.OK, jsonHeaders)
        }
        val vm = ProjectListViewModel()
        val client = mockClient(engine)

        vm.start(client)
        runUntilIdle()
        vm.start(client) // started 守卫：第二次应跳过，不再发请求
        runUntilIdle()

        assertEquals(1, calls)
        assertNotNull(vm.projects)
        assertNull(vm.error)
    }

    @Test
    fun `start_success_sets_projects`() = runVmTest {
        val engine = MockEngine { respond(okJson, HttpStatusCode.OK, jsonHeaders) }
        val vm = ProjectListViewModel()

        vm.start(mockClient(engine))
        runUntilIdle()

        assertEquals(1, vm.projects?.size)
        assertEquals("proj-a", vm.projects?.get(0)?.name)
        assertEquals("copilot", vm.projects?.get(0)?.defaultAgent)
        assertNull(vm.error)
    }

    @Test
    fun `start_failure_sets_error_keeps_data_null`() = runVmTest {
        val engine = MockEngine { respond("", HttpStatusCode.InternalServerError) }
        val vm = ProjectListViewModel()

        vm.start(mockClient(engine))
        runUntilIdle()

        assertNull(vm.projects)
        assertNotNull(vm.error)
    }
}

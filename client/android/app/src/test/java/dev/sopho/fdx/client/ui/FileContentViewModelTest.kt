package dev.sopho.fdx.client.ui

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class FileContentViewModelTest {
    private val okJson =
        """{"path":"a.txt","rev":"work","binary":false,"content":"hello"}"""
    private val binaryJson =
        """{"path":"a.bin","rev":"work","binary":true,"content":""}"""
    private val jsonHeaders = headersOf("Content-Type", "application/json")

    @Test
    fun `start_twice_sends_only_one_request`() = runVmTest {
        var calls = 0
        val engine = MockEngine { _ ->
            calls++
            respond(okJson, HttpStatusCode.OK, jsonHeaders)
        }
        val vm = FileContentViewModel("proj", "a.txt")
        val client = mockClient(engine)

        vm.start(client)
        runUntilIdle()
        vm.start(client)
        runUntilIdle()

        assertEquals(1, calls)
        assertNotNull(vm.file)
        assertNull(vm.error)
    }

    @Test
    fun `start_success_sets_file`() = runVmTest {
        val engine = MockEngine { respond(okJson, HttpStatusCode.OK, jsonHeaders) }
        val vm = FileContentViewModel("proj", "a.txt")

        vm.start(mockClient(engine))
        runUntilIdle()

        assertEquals("hello", vm.file?.content)
        assertEquals(false, vm.file?.binary)
        assertNull(vm.error)
    }

    @Test
    fun `start_success_splits_content_into_lines`() = runVmTest {
        val multiLineJson =
            """{"path":"a.txt","rev":"work","binary":false,"content":"l1\nl2\nl3\n"}"""
        val engine = MockEngine { respond(multiLineJson, HttpStatusCode.OK, jsonHeaders) }
        val vm = FileContentViewModel("proj", "a.txt")

        vm.start(mockClient(engine))
        runUntilIdle()

        // 尾随换行保留一个空行（表示文件末尾换行）
        assertEquals(listOf("l1", "l2", "l3", ""), vm.lines)
    }

    @Test
    fun `start_crlf_content_normalizes_line_endings`() = runVmTest {
        // JSON 里 \r\n 是字面反斜杠，kotlinx 解析成真实 CRLF
        val crlfJson =
            """{"path":"a.txt","rev":"work","binary":false,"content":"l1\r\nl2\r\n"}"""
        val engine = MockEngine { respond(crlfJson, HttpStatusCode.OK, jsonHeaders) }
        val vm = FileContentViewModel("proj", "a.txt")

        vm.start(mockClient(engine))
        runUntilIdle()

        // 行尾无残留 \r
        assertEquals(listOf("l1", "l2", ""), vm.lines)
    }

    @Test
    fun `start_binary_sets_binary_flag`() = runVmTest {
        val engine = MockEngine { respond(binaryJson, HttpStatusCode.OK, jsonHeaders) }
        val vm = FileContentViewModel("proj", "a.bin")

        vm.start(mockClient(engine))
        runUntilIdle()

        assertTrue(vm.file?.binary == true)
        assertEquals("", vm.file?.content)
        assertNull(vm.error)
    }

    @Test
    fun `start_failure_sets_error_keeps_file_null`() = runVmTest {
        val engine = MockEngine { respond("", HttpStatusCode.InternalServerError) }
        val vm = FileContentViewModel("proj", "a.txt")

        vm.start(mockClient(engine))
        runUntilIdle()

        assertNull(vm.file)
        assertEquals("HTTP 500", vm.error)
    }
}

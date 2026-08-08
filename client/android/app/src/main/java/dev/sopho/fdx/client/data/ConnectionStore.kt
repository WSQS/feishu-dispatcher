package dev.sopho.fdx.client.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.serialization.json.Json

/**
 * 连接配置的持久化抽象。存储层（DataStore、Room、内存）都是对这个接口的实现——
 * 对 [Connection] 这个模型的一种「怎么存」的解释，UI/业务不依赖具体存储。
 */
interface ConnectionRepository {
    /** 当前配置流（UI 观察用）；从未保存过时 emit null。 */
    val connection: Flow<Connection?>

    /** 一次性读当前配置（无则 null）。 */
    suspend fun load(): Connection?

    /** 保存（覆盖整个模型）。 */
    suspend fun save(c: Connection)
}

/**
 * 基于 DataStore Preferences 的实现：把整个 [Connection] 序列化成 JSON 存进单一 key。
 *
 * 整体序列化（而非按字段分别存）的意义：加字段时只改 [Connection] + 给默认值，
 * JSON 自动带新字段，**本文件不用改**——存储真正成为模型的投影，不再硬编码字段。
 */
class ConnectionStore(private val context: Context) : ConnectionRepository {
    private val json = Json { ignoreUnknownKeys = true } // 加字段时旧存档的兼容
    private val connectionKey = stringPreferencesKey("connection_json")

    override val connection: Flow<Connection?> =
        context.dataStore.data.map { p ->
            p[connectionKey]?.let { runCatching { json.decodeFromString<Connection>(it) }.getOrNull() }
        }

    override suspend fun load(): Connection? = connection.first()

    override suspend fun save(c: Connection) {
        context.dataStore.edit { it[connectionKey] = json.encodeToString(Connection.serializer(), c) }
    }
}

/** 每进程单一 DataStore 实例（Preferences DataStore 要求 Context 扩展属性单例）。 */
private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "viewer_prefs")

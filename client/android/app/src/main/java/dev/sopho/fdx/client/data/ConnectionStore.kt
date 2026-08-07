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

/** viewer 连接配置（地址 + token），存本地。 */
data class Connection(val url: String, val token: String) {
    /** 简单非空校验（地址和 token 都非空才算有效；正式格式校验留后续）。 */
    val isValid: Boolean get() = url.isNotBlank() && token.isNotBlank()
}

/** 每进程单一 DataStore 实例（Preferences DataStore 要求 Context 扩展属性单例）。 */
private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "viewer_prefs")

/**
 * 持久化 [Connection]（viewer 地址 + token）。Preferences DataStore 封装，协程友好。
 *
 * 用法：\`val store = ConnectionStore(context)\`；\`store.save(url, token)\` / \`store.load()\`。
 */
class ConnectionStore(private val context: Context) {
    private object Keys {
        val URL = stringPreferencesKey("url")
        val TOKEN = stringPreferencesKey("token")
    }

    /** 当前保存的配置流（UI 观察用）；无值时 emit null。 */
    val connectionFlow: Flow<Connection?> =
        context.dataStore.data.map { p ->
            val url = p[Keys.URL]
            val token = p[Keys.TOKEN]
            if (url != null && token != null) Connection(url, token) else null
        }

    /** 一次性读当前配置（无则 null）。启动时填输入框用。 */
    suspend fun load(): Connection? = connectionFlow.first()

    /** 保存（覆盖）。 */
    suspend fun save(url: String, token: String) {
        context.dataStore.edit { it[Keys.URL] = url; it[Keys.TOKEN] = token }
    }
}

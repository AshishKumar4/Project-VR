// vkdisplays.c — enumerate VK_KHR_display objects from current Vulkan session
// Read-only: lists displays + modes per GPU. No acquire.

#define VK_USE_PLATFORM_DISPLAY_KHR
#include <vulkan/vulkan.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    VkApplicationInfo app = {
        .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
        .pApplicationName = "vkdisplays",
        .apiVersion = VK_API_VERSION_1_3,
    };
    const char *exts[] = {
        "VK_KHR_display",
        "VK_KHR_surface",
        "VK_KHR_get_physical_device_properties2",
    };
    VkInstanceCreateInfo ci = {
        .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
        .pApplicationInfo = &app,
        .enabledExtensionCount = sizeof(exts) / sizeof(exts[0]),
        .ppEnabledExtensionNames = exts,
    };
    VkInstance inst;
    if (vkCreateInstance(&ci, NULL, &inst) != VK_SUCCESS) {
        fprintf(stderr, "vkCreateInstance failed\n");
        return 1;
    }
    uint32_t pdc = 0;
    vkEnumeratePhysicalDevices(inst, &pdc, NULL);
    VkPhysicalDevice *pds = calloc(pdc, sizeof(*pds));
    vkEnumeratePhysicalDevices(inst, &pdc, pds);
    for (uint32_t i = 0; i < pdc; i++) {
        VkPhysicalDeviceProperties props;
        vkGetPhysicalDeviceProperties(pds[i], &props);
        printf("\n=== GPU %u: %s ===\n", i, props.deviceName);
        uint32_t dc = 0;
        vkGetPhysicalDeviceDisplayPropertiesKHR(pds[i], &dc, NULL);
        printf("  display count: %u\n", dc);
        if (dc == 0) continue;
        VkDisplayPropertiesKHR *dp = calloc(dc, sizeof(*dp));
        vkGetPhysicalDeviceDisplayPropertiesKHR(pds[i], &dc, dp);
        for (uint32_t j = 0; j < dc; j++) {
            printf("  [%u] %s — physRes %ux%u mm, dims %ux%u\n",
                   j, dp[j].displayName ? dp[j].displayName : "(null)",
                   dp[j].physicalDimensions.width, dp[j].physicalDimensions.height,
                   dp[j].physicalResolution.width, dp[j].physicalResolution.height);
            uint32_t mc = 0;
            vkGetDisplayModePropertiesKHR(pds[i], dp[j].display, &mc, NULL);
            VkDisplayModePropertiesKHR *mp = calloc(mc ? mc : 1, sizeof(*mp));
            vkGetDisplayModePropertiesKHR(pds[i], dp[j].display, &mc, mp);
            for (uint32_t k = 0; k < mc; k++) {
                printf("      mode %u: %ux%u @ %.2f Hz\n",
                       k,
                       mp[k].parameters.visibleRegion.width,
                       mp[k].parameters.visibleRegion.height,
                       mp[k].parameters.refreshRate / 1000.0);
            }
            free(mp);
        }
        free(dp);
    }
    vkDestroyInstance(inst, NULL);
    return 0;
}

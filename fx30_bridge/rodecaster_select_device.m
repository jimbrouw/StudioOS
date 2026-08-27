#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#include <string.h>

static BOOL elementMatches(AXUIElementRef element, NSString *needle,
                           NSString *requiredRole) {
    if (requiredRole != nil) {
        CFTypeRef roleValue = NULL;
        if (AXUIElementCopyAttributeValue(element, kAXRoleAttribute, &roleValue) !=
                kAXErrorSuccess ||
            roleValue == NULL) {
            return NO;
        }
        BOOL roleMatches =
            CFGetTypeID(roleValue) == CFStringGetTypeID() &&
            [(__bridge NSString *)roleValue isEqualToString:requiredRole];
        CFRelease(roleValue);
        if (!roleMatches) return NO;
    }

    CFStringRef attributes[] = {
        kAXTitleAttribute,
        kAXDescriptionAttribute,
        kAXValueAttribute,
        kAXHelpAttribute,
    };

    for (size_t index = 0; index < sizeof(attributes) / sizeof(attributes[0]); index++) {
        CFTypeRef value = NULL;
        if (AXUIElementCopyAttributeValue(element, attributes[index], &value) != kAXErrorSuccess ||
            value == NULL) {
            continue;
        }

        BOOL matches = NO;
        if (CFGetTypeID(value) == CFStringGetTypeID()) {
            NSString *text = (__bridge NSString *)value;
            matches = [text localizedCaseInsensitiveContainsString:needle];
        }
        CFRelease(value);
        if (matches) return YES;
    }
    return NO;
}

static AXUIElementRef findElement(AXUIElementRef element, NSString *needle,
                                  NSString *requiredRole, NSInteger depth) {
    if (depth > 16) return NULL;
    if (elementMatches(element, needle, requiredRole)) {
        return (AXUIElementRef)CFRetain(element);
    }

    CFTypeRef childrenValue = NULL;
    if (AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, &childrenValue) !=
            kAXErrorSuccess ||
        childrenValue == NULL) {
        return NULL;
    }

    AXUIElementRef result = NULL;
    if (CFGetTypeID(childrenValue) == CFArrayGetTypeID()) {
        CFArrayRef children = (CFArrayRef)childrenValue;
        for (CFIndex index = 0; index < CFArrayGetCount(children); index++) {
            AXUIElementRef child =
                (AXUIElementRef)CFArrayGetValueAtIndex(children, index);
            result = findElement(child, needle, requiredRole, depth + 1);
            if (result != NULL) break;
        }
    }
    CFRelease(childrenValue);
    return result;
}

static BOOL clickElementCentre(AXUIElementRef element) {
    CFTypeRef positionValue = NULL;
    CFTypeRef sizeValue = NULL;
    AXError positionError =
        AXUIElementCopyAttributeValue(element, kAXPositionAttribute, &positionValue);
    AXError sizeError =
        AXUIElementCopyAttributeValue(element, kAXSizeAttribute, &sizeValue);

    if (positionError != kAXErrorSuccess || sizeError != kAXErrorSuccess ||
        positionValue == NULL || sizeValue == NULL) {
        if (positionValue != NULL) CFRelease(positionValue);
        if (sizeValue != NULL) CFRelease(sizeValue);
        return NO;
    }

    CGPoint position = CGPointZero;
    CGSize size = CGSizeZero;
    BOOL validPosition = AXValueGetValue((AXValueRef)positionValue,
                                         kAXValueCGPointType, &position);
    BOOL validSize =
        AXValueGetValue((AXValueRef)sizeValue, kAXValueCGSizeType, &size);
    CFRelease(positionValue);
    CFRelease(sizeValue);
    if (!validPosition || !validSize) return NO;

    CGPoint point =
        CGPointMake(position.x + (size.width / 2.0), position.y + (size.height / 2.0));
    CGEventRef move =
        CGEventCreateMouseEvent(NULL, kCGEventMouseMoved, point, kCGMouseButtonLeft);
    CGEventRef down =
        CGEventCreateMouseEvent(NULL, kCGEventLeftMouseDown, point, kCGMouseButtonLeft);
    CGEventRef up =
        CGEventCreateMouseEvent(NULL, kCGEventLeftMouseUp, point, kCGMouseButtonLeft);
    if (move == NULL || down == NULL || up == NULL) {
        if (move != NULL) CFRelease(move);
        if (down != NULL) CFRelease(down);
        if (up != NULL) CFRelease(up);
        return NO;
    }

    CGEventPost(kCGHIDEventTap, move);
    usleep(50000);
    CGEventPost(kCGHIDEventTap, down);
    usleep(100000);
    CGEventPost(kCGHIDEventTap, up);
    CFRelease(move);
    CFRelease(down);
    CFRelease(up);
    return YES;
}

static AXUIElementRef findInRunningApplication(NSRunningApplication *application,
                                               NSString *needle,
                                               NSString *requiredRole) {
    AXUIElementRef appElement =
        AXUIElementCreateApplication(application.processIdentifier);
    AXUIElementRef targetElement =
        findElement(appElement, needle, requiredRole, 0);
    CFRelease(appElement);
    return targetElement;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *bundleIdentifier = @"com.rode.rodecastercomp";
        BOOL buttonMode = argc == 3 && strcmp(argv[1], "--button") == 0;
        if (argc != 1 && !buttonMode) {
            fprintf(stderr,
                    "usage: rodecaster_select_device [--button BUTTON_NAME]\n");
            return 2;
        }
        NSString *needle =
            buttonMode ? [NSString stringWithUTF8String:argv[2]]
                       : @"RØDECaster Pro II";
        NSString *requiredRole = buttonMode ? (__bridge NSString *)kAXButtonRole : nil;

        for (NSInteger attempt = 0; attempt < 80; attempt++) {
            NSArray<NSRunningApplication *> *applications =
                [NSRunningApplication runningApplicationsWithBundleIdentifier:
                                          bundleIdentifier];
            NSRunningApplication *application = applications.firstObject;
            if (application == nil) {
                usleep(250000);
                continue;
            }

            [application activateWithOptions:0];
            AXUIElementRef targetElement =
                findInRunningApplication(application, needle, requiredRole);

            if (targetElement != NULL) {
                BOOL activated = NO;
                if (buttonMode) {
                    activated =
                        AXUIElementPerformAction(targetElement, kAXPressAction) ==
                        kAXErrorSuccess;

                    // The RØDECaster App can report AXPress success without
                    // changing the UI. Verify the named button disappeared;
                    // if it is still present, use a real centre click on the
                    // freshly resolved element instead of reporting success.
                    if (activated) {
                        usleep(350000);
                        AXUIElementRef stillPresent =
                            findInRunningApplication(application, needle,
                                                     requiredRole);
                        if (stillPresent != NULL) {
                            activated = clickElementCentre(stillPresent);
                            CFRelease(stillPresent);
                        }
                    }
                }
                if (!activated) activated = clickElementCentre(targetElement);
                CFRelease(targetElement);
                if (activated) {
                    if (buttonMode) {
                        printf("Pressed RØDECaster App button: %s\n", argv[2]);
                    } else {
                        printf("Selected connected RØDECaster Pro II device.\n");
                    }
                    return 0;
                }
            }
            usleep(250000);
        }

        if (buttonMode) {
            fprintf(stderr, "Timed out finding RØDECaster App button: %s\n", argv[2]);
        } else {
            fprintf(stderr,
                    "Timed out finding the connected RØDECaster Pro II device tile.\n");
        }
        return 1;
    }
}

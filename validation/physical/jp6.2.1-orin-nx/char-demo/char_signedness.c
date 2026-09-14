#include <limits.h>
#include <stdio.h>

int main(void)
{
    char value = -1;

    printf("CHAR_MIN=%d\n", CHAR_MIN);
    printf("plain_char_signedness=%s\n",
           value < 0 ? "signed" : "unsigned");
    printf("value_as_int=%d\n", (int)value);

    return 0;
}
